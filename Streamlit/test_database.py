"""Database regression tests; never modify the project database."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from database import DB_PATH, init_db, open_database


class DatabaseTests(unittest.TestCase):
    def test_existing_database_migration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'test.db'
            source = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
            target = sqlite3.connect(path)
            source.backup(target)
            source.close()
            before_users = target.execute('SELECT * FROM users').fetchall()
            before_docs = target.execute('SELECT * FROM documents').fetchall()
            target.close()
            init_db(path)
            with closing(open_database(path)) as conn:
                self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
                self.assertEqual(conn.execute('SELECT * FROM users').fetchall(), before_users)
                self.assertEqual(conn.execute('SELECT * FROM documents').fetchall(), before_docs)
                count = conn.execute('SELECT COUNT(*) FROM migration_archive').fetchone()[0]
            init_db(path)
            with closing(open_database(path)) as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM migration_archive').fetchone()[0], count)
            self.assertEqual(len(list((Path(folder) / 'database_backups').glob('*.db'))), 1)

    def test_fresh_database_and_chat_cascade(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'test.db'
            init_db(path)
            conn = open_database(path)
            try:
                self.assertEqual(conn.execute('PRAGMA foreign_keys').fetchone()[0], 1)
                user = conn.execute('SELECT id FROM users LIMIT 1').fetchone()[0]
                session = conn.execute('INSERT INTO chat_sessions(user_id) VALUES (?)', (user,)).lastrowid
                conn.execute("INSERT INTO chat_messages(session_id,role,content) VALUES (?, 'user', 'test')", (session,))
                conn.execute('DELETE FROM chat_sessions WHERE id=?', (session,))
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM chat_messages').fetchone()[0], 0)
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute("INSERT INTO chat_messages(session_id,role,content) VALUES (999, 'user', 'bad')")
            finally:
                conn.close()


if __name__ == '__main__':
    unittest.main()
