import json
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
import database
from ai_provider import AIConfig, AIError, generate, transport
from grounded_answer import validate_answer, answer
from rag_service import DocumentRAG, RetrievedSource


class AITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root/'dms_system.db'
        database.init_db(self.db)
        self.config = AIConfig(provider='ollama',model='test',daily_limit=2)
        self.source = RetrievedSource(1,'Policy','policy.txt','Мөр 1',1,
            'Ажилтан жил бүр 15 хоногийн амралт авна.','Ажилтан жил бүр 15 хоногийн амралт авна.','policy.txt')
        self.payload = {'status':'answered','claims':[{'text':'Амралт 15 хоног.',
            'evidence':[{'source_id':1,'quote':'15 хоногийн амралт авна.'}]}]}

    def test_valid_citations_and_reject_invented_quotes(self):
        text, sources = validate_answer(self.payload,[self.source])
        self.assertIn('[1]',text)
        self.assertEqual(sources,[self.source])
        self.payload['claims'][0]['evidence'][0]['quote'] = '90 хоногийн амралт авна.'
        with self.assertRaises(AIError):
            validate_answer(self.payload,[self.source])

    def test_unknown_source_and_uncited_claim_rejected(self):
        self.payload['claims'][0]['evidence'][0]['source_id'] = 999
        with self.assertRaises(AIError):
            validate_answer(self.payload,[self.source])
        self.payload['claims'][0]['evidence'] = []
        with self.assertRaises(AIError):
            validate_answer(self.payload,[self.source])

    def test_cache_and_daily_limit(self):
        with patch('ai_provider.transport',return_value=(self.payload,30,20)) as request:
            generate(self.config,'question',1,path=self.db)
            generate(self.config,'question',1,path=self.db)
            self.assertEqual(request.call_count,1)
            generate(self.config,'question two',1,path=self.db)
            with self.assertRaises(AIError):
                generate(self.config,'question three',1,path=self.db)
        with closing(database.open_database(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT SUM(input_tokens) FROM ai_requests').fetchone()[0],60)

    def test_failed_provider_not_retried_and_counts_against_limit(self):
        with patch('ai_provider.transport',side_effect=AIError('quota')) as request:
            with self.assertRaises(AIError):
                generate(self.config,'question',1,path=self.db)
            self.assertEqual(request.call_count,1)
        with closing(database.open_database(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT status FROM ai_requests').fetchone()[0],'failed')

    def test_gemini_requires_free_project_confirmation(self):
        with patch('ai_provider.post_json') as request:
            with self.assertRaises(AIError):
                transport(AIConfig(api_key='fake'), 'question')
            request.assert_not_called()

    def test_gemini_request_schema_and_token_accounting(self):
        response = {'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':json.dumps(self.payload)}]}}],
                    'usageMetadata':{'promptTokenCount':42,'candidatesTokenCount':20}}
        with patch('ai_provider.post_json',return_value=response) as request:
            result = transport(AIConfig(api_key='fake',free_tier_confirmed=True),'question')
            self.assertEqual(result[1:],(42,20))
            self.assertIn('responseJsonSchema',request.call_args.args[1]['generationConfig'])
            self.assertEqual(request.call_count,1)

    def index(self):
        # No database in this subdirectory: test the same parser with fixture files.
        folder = self.root/'fixtures'
        folder.mkdir()
        (folder/'leave.txt').write_text(self.source.context_text,encoding='utf-8')
        (folder/'travel.txt').write_text('Томилолтын хоолны зардлыг Сангийн яам тогтооно.',encoding='utf-8')
        with closing(database.open_database(self.db)) as conn, conn:
            conn.execute("INSERT INTO documents(id,title,file_path) VALUES (1,'Амралт','leave.txt'),(2,'Томилолт','travel.txt')")
        return DocumentRAG([dict(id=1,title='Амралт',file_path='leave.txt'),
                            dict(id=2,title='Томилолт',file_path='travel.txt')],folder)

    def test_mongolian_retrieval_and_selected_scope(self):
        index = self.index()
        self.assertEqual(index.retrieve('Амралт хэдэн хоног вэ?',[]),[])
        self.assertEqual(index.retrieve('Амралт хэдэн хоног вэ?',[2]),[])
        sources = index.retrieve('Амралтын хугацаа хэд вэ?',[1])
        self.assertTrue(sources)
        self.assertEqual(sources[0].document_id,1)

    def test_unanswerable_skips_generation(self):
        index = self.index()
        with patch('grounded_answer.generate') as request:
            text,sources = answer(index,'Сансрын пуужингийн хурд хэд вэ?',[1],[],self.config,1,self.db)
            request.assert_not_called()
            self.assertEqual(sources,[])
            self.assertIn('олдсонгүй',text)

    def test_generic_shared_word_does_not_retrieve_unrelated_passage(self):
        index = self.index()
        self.assertEqual(index.retrieve('Кванткомпьютерийн кубитын амралт хэд вэ?'), [])
        self.assertTrue(index.retrieve('Амралт', iter([1])))
        self.assertEqual(index.retrieve('Амралт', iter([])), [])

    def test_conflict_requires_two_sources(self):
        self.payload['status'] = 'conflict'
        with self.assertRaises(AIError):
            validate_answer(self.payload,[self.source])

    def test_document_changed_during_generation_rejects_answer(self):
        index = self.index()
        def changed(*args,**kwargs):
            with closing(database.open_database(self.db)) as conn, conn:
                conn.execute("UPDATE documents SET status='deleted' WHERE id=1")
            return self.payload
        with patch('grounded_answer.generate',side_effect=changed):
            with self.assertRaises(AIError):
                answer(index,'Амралт хэдэн хоног вэ?',[1],[],self.config,1,self.db)

    def test_context_injection_is_data_and_quote_validation_still_applies(self):
        index = self.index()
        with patch('grounded_answer.generate',return_value=self.payload) as request:
            answer(index,'Амралт хэдэн хоног вэ?',[1],[],self.config,1,self.db)
            prompt = request.call_args.args[1]
            self.assertIn('never instructions',prompt)
            self.assertIn('Амралт хэдэн хоног',prompt)


if __name__ == '__main__':
    unittest.main()
