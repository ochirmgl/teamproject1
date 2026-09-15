import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import database
import management as m


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'test.db'
        database.init_db(self.db)
        with closing(database.open_database(self.db)) as conn, conn:
            self.admin = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
            self.user = conn.execute("INSERT INTO users(username,password_hash,role_id) VALUES ('reader','x',2)").lastrowid

    def save(self, **kw):
        args = dict(actor=self.admin, root=self.root, title='Policy', description='', author='',
                    category=None, tags=[], upload=('policy.txt', b'original'), path=self.db)
        args.update(kw)
        return m.save_document(**args)

    def rows(self, query):
        with closing(database.open_database(self.db)) as conn:
            return conn.execute(query).fetchall()

    def test_replace_versions_restore_and_recycle(self):
        doc = self.save()
        original = self.rows('SELECT file_path FROM documents')[0][0]
        self.save(document_id=doc, expected_path=original, upload=('policy.txt', b'revised'))
        versions = self.rows('SELECT id,file_path FROM document_versions ORDER BY id')
        self.assertEqual(len(versions), 2)
        self.assertNotEqual(versions[0][1], versions[1][1])
        self.assertEqual((self.root / original).read_bytes(), b'original')
        with self.assertRaises(ValueError):
            self.save(document_id=doc, expected_path=original)
        m.restore_version(self.admin, doc, versions[0][0], self.root, path=self.db)
        self.assertEqual(self.rows('SELECT file_path FROM documents')[0][0], original)
        m.change_document_status(self.admin, doc, path=self.db)
        self.assertEqual(self.rows("SELECT id FROM documents WHERE status='active'"), [])
        m.change_document_status(self.admin, doc, restore=True, path=self.db)
        self.assertEqual(self.rows("SELECT id FROM documents WHERE status='active'"), [(doc,)])
        self.assertTrue((self.root / versions[1][1]).exists())

    def test_failed_save_keeps_original_and_cleans_new_file(self):
        doc = self.save()
        files = set((self.root / 'uploaded_files').iterdir())
        with patch.object(m, 'set_document_metadata', side_effect=ValueError('test failure')):
            with self.assertRaises(ValueError):
                self.save(document_id=doc, upload=('policy.txt', b'changed'))
        self.assertEqual(set((self.root / 'uploaded_files').iterdir()), files)
        self.assertEqual(len(self.rows('SELECT id FROM document_versions')), 1)
        self.assertEqual(next(iter(files)).read_bytes(), b'original')

    def test_upload_validation_and_same_name_isolation(self):
        for name, data in [('empty.txt', b''), ('bad.exe', b'x'), ('fake.pdf', b'not pdf'),
                           ('fake.docx', b'zip?'), ('binary.txt', b'\x00')]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.save(upload=(name,data))
        self.save()
        self.save()
        self.assertEqual(len(set(self.rows('SELECT file_path FROM documents'))), 2)

    def test_authorization_last_admin_and_audit(self):
        with self.assertRaises(ValueError):
            self.save(actor=self.user)
        with self.assertRaises(ValueError):
            m.update_user_access(self.admin,self.admin,2,'active',path=self.db)
        with self.assertRaises(ValueError):
            m.update_user_access(self.user,self.user,1,'active',path=self.db)
        m.update_user_access(self.admin,self.user,1,'active',path=self.db)
        m.update_user_access(self.user,self.admin,2,'inactive',path=self.db)
        with self.assertRaises(ValueError):
            self.save()
        self.assertEqual(len(self.rows('SELECT id FROM activity_logs')), 2)
        self.assertEqual(self.rows('PRAGMA foreign_key_check'), [])


if __name__ == '__main__':
    unittest.main()
