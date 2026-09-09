import sqlite3 # SQLite санг ашиглахад хэрэглэдэг
import hashlib
import json
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


DB_PATH = Path(__file__).resolve().parent / "dms_system.db"

# Added by Ochir: one connection policy for every database operation.
def open_database(path=None):
    conn = sqlite3.connect(path or DB_PATH, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(path=None):
    # 'dms_system.db' нэртэй файл үүсгэнэ
    conn = open_database(path)
    cursor = conn.cursor()

    # 1. ROLES хүснэгт
    cursor.execute('''CREATE TABLE IF NOT EXISTS roles (  
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role_name TEXT NOT NULL
    )''') # Хэрэв roles гэсэн хүснэгт байхгүй бол шинийг үүсгэ гэсэн зоманд юм, AUTOINCREMENT гэдэг нь хэрэглэгч өөрөө дугаар өгөөд явах юм аутоматаар дугаарлаад явах юм.

    # ЭНД НЭМЭХ: Хүснэгт үүсгэсний дараа, өгөгдлийг нь оруулах
    cursor.execute("INSERT OR IGNORE INTO roles (id, role_name) VALUES (1, 'Admin')")
    cursor.execute("INSERT OR IGNORE INTO roles (id, role_name) VALUES (2, 'User')")
    
    # 2. USERS хүснэгт
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role_id INTEGER,
        status TEXT DEFAULT 'active',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (role_id) REFERENCES roles (id)
    )''')

    # 3. CATEGORIES хүснэгт
    cursor.execute('''CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT
    )''')

    # 4. DOCUMENTS хүснэгт
    cursor.execute('''CREATE TABLE IF NOT EXISTS documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT,
        category_id INTEGER,
        file_path TEXT NOT NULL,
        file_type TEXT,
        source_author TEXT,
        uploaded_by INTEGER,
        upload_date DATETIME DEFAULT CURRENT_TIMESTAMP,
        status TEXT DEFAULT 'active',
        FOREIGN KEY (category_id) REFERENCES categories (id),
        FOREIGN KEY (uploaded_by) REFERENCES users (id)
    )''')

    # 5. TAGS хүснэгт
    cursor.execute('''CREATE TABLE IF NOT EXISTS tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL
    )''')

    # 6. DOCUMENT_TAGS хүснэгт
    cursor.execute('''CREATE TABLE IF NOT EXISTS document_tags (
        document_id INTEGER,
        tag_id INTEGER,
        PRIMARY KEY (document_id, tag_id),
        FOREIGN KEY (document_id) REFERENCES documents (id),
        FOREIGN KEY (tag_id) REFERENCES tags (id)
    )''')

    # 7. SEARCH_HISTORY хүснэгт
    cursor.execute('''CREATE TABLE IF NOT EXISTS search_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        search_query TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (id)
    )''')

    # 8. ACTIVITY_LOGS хүснэгт
    cursor.execute('''CREATE TABLE IF NOT EXISTS activity_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        document_id INTEGER,
        action TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (id),
        FOREIGN KEY (document_id) REFERENCES documents (id)
    )''')

    # 9. CHAT_SESSIONS хүснэгт
    # Added by Ochir: хэрэглэгч бүрийн тусдаа чатын жагсаалтыг хадгална.
    cursor.execute('''CREATE TABLE IF NOT EXISTS chat_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL DEFAULT 'Шинэ чат',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )''')

    # 10. CHAT_MESSAGES хүснэгт
    # Added by Ochir: асуулт, AI хариулт болон эх сурвалжийн JSON-ийг хадгална.
    cursor.execute('''CREATE TABLE IF NOT EXISTS chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
        content TEXT NOT NULL,
        sources_json TEXT NOT NULL DEFAULT '[]',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (session_id) REFERENCES chat_sessions (id) ON DELETE CASCADE
    )''')

    # 11. CHAT_SESSION_DOCUMENTS хүснэгт
    # Added by Ochir: тухайн чатад сонгосон баримтуудыг дахин нээхэд сэргээнэ.
    cursor.execute('''CREATE TABLE IF NOT EXISTS chat_session_documents (
        session_id INTEGER NOT NULL,
        document_id INTEGER NOT NULL,
        PRIMARY KEY (session_id, document_id),
        FOREIGN KEY (session_id) REFERENCES chat_sessions (id) ON DELETE CASCADE,
        FOREIGN KEY (document_id) REFERENCES documents (id) ON DELETE CASCADE
    )''')

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_sessions_user ON chat_sessions(user_id, updated_at)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id, id)"
    )

    # --- АНХНЫ АДМИН ХЭРЭГЛЭГЧИЙГ АВТОМАТААР ҮҮСГЭХ ---
    # Нууц үгийг sha256 ашиглан hash хийнэ (Жишээ нь нууц үг: admin123)
    admin_password_hash = hashlib.sha256("admin123".encode()).hexdigest()
    
    cursor.execute('''
        INSERT OR IGNORE INTO users (username, password_hash, role_id, status)
        VALUES (?, ?, ?, ?)
    ''', ('admin', admin_password_hash, 1, 'active'))

    conn.commit()
    conn.close()
    migrate_database(path)


def migrate_database(path=None):
    """Back up and repair legacy relationships without losing historical rows."""
    db_path = Path(path or DB_PATH)
    conn = open_database(db_path)
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] >= 1:
            return
        backup_dir = db_path.parent / "database_backups"
        backup_dir.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        with closing(sqlite3.connect(backup_dir / f"before_v1_{stamp}.db")) as backup:
            conn.backup(backup)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE IF NOT EXISTS migration_archive (
            id INTEGER PRIMARY KEY, source_table TEXT NOT NULL,
            original_row_json TEXT NOT NULL, reason TEXT NOT NULL,
            archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        # Retain broken legacy records before removing invalid relationships.
        repairs = [
            ("chat_messages", "session_id NOT IN (SELECT id FROM chat_sessions)", None),
            ("chat_session_documents", "session_id NOT IN (SELECT id FROM chat_sessions) OR document_id NOT IN (SELECT id FROM documents)", None),
            ("activity_logs", "document_id IS NOT NULL AND document_id NOT IN (SELECT id FROM documents)", "document_id"),
        ]
        for table, condition, nullable_column in repairs:
            cursor = conn.execute(f"SELECT rowid AS legacy_rowid, * FROM {table} WHERE {condition}")
            names = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                conn.execute("INSERT INTO migration_archive (source_table, original_row_json, reason) VALUES (?, ?, ?)",
                             (table, json.dumps(dict(zip(names, row)), ensure_ascii=False), "Missing referenced record"))
            if nullable_column:
                conn.execute(f"UPDATE {table} SET {nullable_column} = NULL WHERE {condition}")
            else:
                conn.execute(f"DELETE FROM {table} WHERE {condition}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_category ON documents(category_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_document_tags_tag ON document_tags(tag_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_document ON activity_logs(document_id)")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"Database migration stopped: {len(violations)} unresolved relationships")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

if __name__ == "__main__":
    init_db()
