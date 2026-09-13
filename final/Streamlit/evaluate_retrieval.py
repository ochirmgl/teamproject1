"""Run a small retrieval smoke evaluation on the bundled public documents; no AI API."""
from contextlib import closing
import json
from pathlib import Path
from database import init_db, open_database
from rag_service import DocumentRAG

CASES = [
    ('Албан томилолтын хоолны зардлыг хэн тогтоох вэ?', 'ТОМИЛОЛТ'),
    ('Ээлжийн амралтын цалин тооцох журам юу вэ?', 'амралт'),
    ('Хөдөлмөрийн аюулгүй ажиллагааны зааварчилгааг хэрхэн хийх вэ?', 'АЮУЛГҮЙ'),
    ('Гэрч хохирогчийн мэдээллийн нууцлалын гэрээ юу вэ?', 'ГЭРЧ'),
    ('Кванткомпьютерийн кубитын давтамж хэд вэ?', None),
]

if __name__ == '__main__':
    init_db()
    with closing(open_database()) as conn:
        rows = conn.execute("SELECT id,title,file_path,file_type FROM documents WHERE status='active'").fetchall()
    records = [dict(zip(('id','title','file_path','file_type'),row)) for row in rows]
    index = DocumentRAG(records,Path(__file__).parent)
    results = []
    for question, expected in CASES:
        sources = index.retrieve(question,limit=5)
        passed = not sources if expected is None else any(expected.casefold() in s.title.casefold() for s in sources)
        results.append({'question':question,'passed':passed,'retrieved_titles':[s.title for s in sources]})
    print(json.dumps({'passed':sum(r['passed'] for r in results),'total':len(results),'results':results},ensure_ascii=False,indent=2))
    raise SystemExit(0 if all(r['passed'] for r in results) else 1)
