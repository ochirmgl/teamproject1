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


def post_json(url, payload, headers=None):
    request = urllib.request.Request(url, json.dumps(payload).encode(),
        {'Content-Type':'application/json', **(headers or {})}, method='POST')
    try:
        with urllib.request.urlopen(request,timeout=90) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise AIError('AI үйлчилгээний үнэгүй хэрэглээний хязгаарт хүрлээ. Дараа дахин оролдоно уу.') from None
        if exc.code in (401,403):
            raise AIError('AI холболтын түлхүүр эсвэл хандах эрхийг шалгана уу.') from None
        raise AIError(f'AI үйлчилгээ хүсэлтийг гүйцэтгэсэнгүй (HTTP {exc.code}).') from None
    except (OSError, ValueError):
        raise AIError('AI үйлчилгээтэй холбогдож чадсангүй. Дахин оролдоно уу.') from None


def transport(config, prompt):
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
    if config.provider == 'gemini' and (not config.free_tier_confirmed or not config.api_key):
        raise AIError('Үнэгүй Gemini төслийг эхлээд тохируулна уу.')
    key = hashlib.sha256((config.provider+'|'+config.model+'|'+str(user_id)+'|'+prompt).encode()).hexdigest()
    with closing(open_database(path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        account = conn.execute("SELECT 1 FROM users WHERE id=? AND status='active'",(user_id,)).fetchone()
        if not account:
            raise AIError('Дахин нэвтэрнэ үү.')
        cached = conn.execute("SELECT response_json FROM ai_requests WHERE cache_key=? AND status='complete' ORDER BY id DESC LIMIT 1",(key,)).fetchone()
        if cached:
            conn.rollback()
            return json.loads(cached[0])
        used = conn.execute("SELECT COUNT(*) FROM ai_requests WHERE created_at>=date('now')").fetchone()[0]
        if used >= max(0,min(config.daily_limit,1000)):
            raise AIError('Системийн өдрийн AI хүсэлтийн хязгаарт хүрлээ.')
        request_id = conn.execute("INSERT INTO ai_requests(user_id,provider,model,cache_key,status) VALUES (?,?,?,?,'pending')",
            (user_id,config.provider,config.model,key)).lastrowid
        conn.commit()
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
