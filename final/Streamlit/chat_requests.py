"""Durable, single-flight chat requests. No automatic provider retries."""
import json
from contextlib import closing
from database import open_database


def ensure_schema(path):
    with closing(open_database(path)) as conn, conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS chat_jobs (
            request_id TEXT PRIMARY KEY, session_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT,
            FOREIGN KEY(session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id))''')
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS chat_single_pending ON chat_jobs(session_id) WHERE status='pending'")


def recover(path, session_id, user_id):
    """Expire abandoned jobs after ten minutes; late workers cannot overwrite them."""
    with closing(open_database(path)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        stale = conn.execute("SELECT request_id FROM chat_jobs WHERE session_id=? AND user_id=? AND status='pending' AND created_at < datetime('now','-10 minutes')", (session_id, user_id)).fetchall()
        for (request_id,) in stale:
            conn.execute("UPDATE chat_jobs SET status='failed',finished_at=CURRENT_TIMESTAMP WHERE request_id=?", (request_id,))
            conn.execute("INSERT INTO chat_messages(session_id,role,content,sources_json) VALUES (?,'assistant',?,'[]')", (session_id, 'Хүсэлт тасалдсан эсвэл хугацаа хэтэрсэн. Асуулт хадгалагдсан. Дахин илгээвэл шинэ AI хүсэлт тооцогдож болно.'))
        return conn.execute("SELECT 1 FROM chat_jobs WHERE session_id=? AND user_id=? AND status='pending'", (session_id,user_id)).fetchone() is not None


def begin(path, request_id, session_id, user_id, question):
    with closing(open_database(path)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        owner = conn.execute('SELECT title FROM chat_sessions WHERE id=? AND user_id=?', (session_id,user_id)).fetchone()
        if not owner:
            raise PermissionError('Chat session unavailable')
        if conn.execute('SELECT 1 FROM chat_jobs WHERE request_id=?', (request_id,)).fetchone():
            return False
        if conn.execute("SELECT 1 FROM chat_jobs WHERE session_id=? AND status='pending'", (session_id,)).fetchone():
            return False
        conn.execute("INSERT INTO chat_jobs(request_id,session_id,user_id,status) VALUES (?,?,?,'pending')", (request_id,session_id,user_id))
        conn.execute("INSERT INTO chat_messages(session_id,role,content,sources_json) VALUES (?,'user',?,'[]')", (session_id,question))
        title = owner[0]
        if title.lower().strip(' !?.') in {'шинэ чат','hi','hello','hey','сайн уу','сайн байна уу'}:
            title = ' '.join(question.split())[:60] or 'Шинэ чат'
        conn.execute('UPDATE chat_sessions SET title=?,updated_at=CURRENT_TIMESTAMP WHERE id=?', (title,session_id))
        return True


def finish(path, request_id, user_id, answer, sources=None, failed=False):
    with closing(open_database(path)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        job = conn.execute("SELECT session_id FROM chat_jobs WHERE request_id=? AND user_id=? AND status='pending'", (request_id,user_id)).fetchone()
        if not job:
            return False
        conn.execute("INSERT INTO chat_messages(session_id,role,content,sources_json) VALUES (?,'assistant',?,?)", (job[0],answer,json.dumps(sources or [],ensure_ascii=False)))
        conn.execute('UPDATE chat_jobs SET status=?,finished_at=CURRENT_TIMESTAMP WHERE request_id=?', ('failed' if failed else 'complete',request_id))
        conn.execute('UPDATE chat_sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=?', (job[0],))
        return True
