"""Free-tier Gemini / local Ollama, single requests with local quota accounting."""
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import urllib.request
import urllib.error
from database import open_database


class AIError(RuntimeError):
    pass


SCHEMA = {'type':'object','properties': {
    'status': {'type':'string','enum':['answered','not_found','conflict']},
    'claims': {'type':'array','items': {'type':'object','properties': {
        'text': {'type':'string'},
        'evidence': {'type':'array','items': {'type':'object','properties': {
            'source_id': {'type':'integer'}, 'quote': {'type':'string'}},
            'required':['source_id','quote']}}},'required':['text','evidence']}}},
    'required':['status','claims']}


@dataclass
class AIConfig:
    provider: str = 'gemini'
    model: str = 'gemini-2.5-flash'
    api_key: str = ''
    free_tier_confirmed: bool = False
    daily_limit: int = 20
    max_output: int = 2048
    user_daily_limit: int = 10
    last_response_kind: str = 'local'


def post_json(url, payload, headers=None):
    request = urllib.request.Request(url, json.dumps(payload).encode(),
        {'Content-Type':'application/json', 'Accept':'application/json',
         'User-Agent':'DMS-Document-Assistant/1.0', **(headers or {})}, method='POST')
    try:
        with urllib.request.urlopen(request,timeout=90) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise AIError('AI үйлчилгээний үнэгүй хэрэглээний хязгаарт хүрлээ. Дараа дахин оролдоно уу.') from None
        if exc.code == 401:
            raise AIError('HTTP 401: AI үйлчилгээ түлхүүрийг зөвшөөрсөнгүй. Streamlit Secrets дахь бүтэн түлхүүр болон тохиргооны эх үүсвэрийг шалгана уу.') from None
        if exc.code == 403:
            # Never show the raw response: upstream messages may echo request data.
            try:
                body = json.loads(exc.read(8192))
                code = body.get('error', {}).get('code')
            except (ValueError, AttributeError, OSError):
                code = None
            if code in ('model_permission_blocked_org','model_permission_blocked_project'):
                raise AIError('HTTP 403: Groq байгууллага эсвэл төслийн Model permissions энэ загварыг хориглосон байна.') from None
            raise AIError('HTTP 403: Хүсэлт хориглогдлоо. Төслийн эрх, загварын зөвшөөрөл эсвэл сүлжээний хамгаалалтын хориг байж болно; энэ нь заавал буруу түлхүүр гэсэн үг биш.') from None
        raise AIError(f'AI үйлчилгээ хүсэлтийг гүйцэтгэсэнгүй (HTTP {exc.code}).') from None
    except (OSError, ValueError):
        raise AIError('AI үйлчилгээтэй холбогдож чадсангүй. Дахин оролдоно уу.') from None


def transport(config, prompt):
    if config.provider == 'groq':
        if not config.free_tier_confirmed or not config.api_key:
            raise AIError('Groq үнэгүй тохиргоо болон API түлхүүрийг шалгана уу.')
        if config.model not in ('openai/gpt-oss-120b', 'openai/gpt-oss-20b'):
            raise AIError('Дэмжигдэх Groq загварыг сонгоно уу.')
        schema = json.loads(json.dumps(SCHEMA))
        def strict_objects(node):
            if isinstance(node, dict):
                if node.get('type') == 'object':
                    node['additionalProperties'] = False
                for value in node.values():
                    strict_objects(value)
            elif isinstance(node, list):
                for value in node:
                    strict_objects(value)
        strict_objects(schema)
        response = post_json('https://api.groq.com/openai/v1/chat/completions',
            {'model':config.model, 'messages':[{'role':'user','content':prompt}],
             'temperature':0, 'reasoning_effort':'low', 'max_completion_tokens':config.max_output,
             'response_format':{'type':'json_schema','json_schema':{
                 'name':'document_answer','strict':True,'schema':schema}}},
            {'Authorization':'Bearer ' + config.api_key})
        choices = response.get('choices', [])
        if not choices or choices[0].get('finish_reason') != 'stop' or choices[0].get('message', {}).get('refusal'):
            raise AIError('AI хариулт бүрэн ирсэнгүй. Автоматаар дахин хүсэлт илгээхгүй.')
        usage = response.get('usage', {})
        return json.loads(choices[0]['message']['content']), int(usage.get('prompt_tokens',0)), int(usage.get('completion_tokens',0))
    if config.provider == 'gemini':
        if not config.free_tier_confirmed or not config.api_key:
            raise AIError('Үнэгүй Gemini төслийн тохиргоог администратор баталгаажуулна уу.')
        if config.model not in ('gemini-2.5-flash','gemini-2.5-flash-lite'):
            raise AIError('Энэ Gemini загвар үнэгүй тохиргооны жагсаалтад байхгүй байна.')
        response = post_json(f'https://generativelanguage.googleapis.com/v1beta/models/{config.model}:generateContent',
            {'contents':[{'parts':[{'text':prompt}]}],
             'generationConfig': {'responseMimeType':'application/json','responseJsonSchema':SCHEMA,
                 'maxOutputTokens':config.max_output, 'temperature':0,
                 'thinkingConfig':{'thinkingBudget':0}}}, {'x-goog-api-key':config.api_key})
        candidates = response.get('candidates',[])
        if not candidates or candidates[0].get('finishReason') != 'STOP':
            raise AIError('AI хариулт бүрэн ирсэнгүй. Автоматаар дахин хүсэлт илгээхгүй.')
        raw = ''.join(p.get('text','') for p in candidates[0].get('content',{}).get('parts',[]))
        usage = response.get('usageMetadata',{})
        return json.loads(raw), int(usage.get('promptTokenCount',0)), int(usage.get('candidatesTokenCount',0)) + int(usage.get('thoughtsTokenCount',0))
    if config.provider == 'ollama':
        response = post_json('http://127.0.0.1:11434/api/chat',
            {'model':config.model,'stream':False,'think':False,'format':SCHEMA,
             'messages':[{'role':'user','content':prompt}],
             'options':{'temperature':0,'num_predict':config.max_output}})
        if not response.get('done') or response.get('done_reason') == 'length':
            raise AIError('Дотоод AI хариултаа бүрэн дуусгасангүй.')
        return json.loads(response['message']['content']),int(response.get('prompt_eval_count',0)),int(response.get('eval_count',0))
    raise AIError('AI provider тохиргоо буруу байна.')


def generate(config, prompt, user_id, path=None):
    if len(prompt) > 24000:
        raise AIError('Асуулт болон эх сурвалжийн хэмжээ хэтэрсэн байна.')
    if config.provider in ('gemini', 'groq') and (not config.free_tier_confirmed or not config.api_key):
        raise AIError('Үнэгүй AI үйлчилгээ болон түлхүүрийг эхлээд тохируулна уу.')
    key = hashlib.sha256((config.provider+'|'+config.model+'|'+str(user_id)+'|'+prompt).encode()).hexdigest()
    with closing(open_database(path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        account = conn.execute("SELECT 1 FROM users WHERE id=? AND status='active'",(user_id,)).fetchone()
        if not account:
            raise AIError('Дахин нэвтэрнэ үү.')
        cached = conn.execute("SELECT response_json FROM ai_requests WHERE cache_key=? AND status='complete' ORDER BY id DESC LIMIT 1",(key,)).fetchone()
        if cached:
            conn.rollback()
            config.last_response_kind = 'cache'
            return json.loads(cached[0])
        user_used = conn.execute("SELECT COUNT(*) FROM ai_requests WHERE user_id=? AND created_at>=date('now')", (user_id,)).fetchone()[0]
        if user_used >= config.user_daily_limit:
            raise AIError('Таны өдрийн AI хүсэлтийн хязгаарт хүрлээ.')
        used = conn.execute("SELECT COUNT(*) FROM ai_requests WHERE created_at>=date('now')").fetchone()[0]
        if used >= max(0,min(config.daily_limit,1000)):
            raise AIError('Системийн өдрийн AI хүсэлтийн хязгаарт хүрлээ.')
        request_id = conn.execute("INSERT INTO ai_requests(user_id,provider,model,cache_key,status) VALUES (?,?,?,?,'pending')",
            (user_id,config.provider,config.model,key)).lastrowid
        conn.commit()
        config.last_response_kind = 'api'
        try:
            answer, input_tokens, output_tokens = transport(config,prompt)
            conn.execute("UPDATE ai_requests SET status='complete',response_json=?,input_tokens=?,output_tokens=? WHERE id=?",
                         (json.dumps(answer,ensure_ascii=False),input_tokens,output_tokens,request_id))
            conn.commit()
            return answer
        except Exception as exc:
            conn.execute("UPDATE ai_requests SET status='failed' WHERE id=?",(request_id,))
            conn.commit()
            if isinstance(exc,AIError):
                raise
            raise AIError('AI хариултын бүтцийг уншиж чадсангүй. Дахин оролдоно уу.') from None
