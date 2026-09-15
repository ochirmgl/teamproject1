import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import metadata
from database import init_db, open_database


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "test.db"
        init_db(self.path)
        with closing(open_database(self.path)) as conn, conn:
            conn.execute("INSERT INTO documents(title,file_path) VALUES ('Policy','policy.txt')")

    def test_migration_merges_whitespace_duplicates_without_losing_links(self):
        with closing(open_database(self.path)) as conn, conn:
            conn.execute('DROP INDEX ux_tags_name_nocase')
            conn.execute('DROP INDEX ux_categories_name_nocase')
            conn.execute("INSERT INTO categories(id,name) VALUES (1,' Policy '),(2,'Policy')")
            conn.execute("INSERT INTO tags(id,name) VALUES (1,' Staff '),(2,'Staff')")
            conn.execute('UPDATE documents SET category_id=2 WHERE id=1')
            conn.execute('INSERT INTO document_tags(document_id,tag_id) VALUES (1,2)')
            conn.execute('PRAGMA user_version=1')
        init_db(self.path)
        self.assertEqual(metadata.document_metadata(1, self.path), (1, [1]))
        self.assertEqual(metadata.list_categories(self.path)[0][1], 'Policy')
        self.assertEqual(metadata.list_tags(self.path)[0][1], 'Staff')

    def test_cyrillic_duplicates_and_rename(self):
        self.assertTrue(metadata.create_category("  Дотоод   журам ", path=self.path)[0])
        self.assertFalse(metadata.create_category("ДОТООД ЖУРАМ", path=self.path)[0])
        self.assertFalse(metadata.create_category("   ", path=self.path)[0])
        self.assertTrue(metadata.create_tag("Ажилтан", self.path)[0])
        self.assertFalse(metadata.create_tag("АЖИЛТАН", self.path)[0])
        category_id = metadata.list_categories(self.path)[0][0]
        tag_id = metadata.list_tags(self.path)[0][0]
        self.assertTrue(metadata.update_category(category_id, "Бодлого", path=self.path)[0])
        self.assertTrue(metadata.update_tag(tag_id, "Хүний нөөц", self.path)[0])
        self.assertEqual(metadata.list_categories(self.path)[0][1], "Бодлого")

    def test_assign_replace_clear_and_invalid_assignment_rollback(self):
        metadata.create_category("Policy", path=self.path)
        metadata.create_tag("Staff", self.path)
        category_id = metadata.list_categories(self.path)[0][0]
        tag_id = metadata.list_tags(self.path)[0][0]
        with closing(open_database(self.path)) as conn, conn:
            metadata.set_document_metadata(conn, 1, category_id, [tag_id, tag_id])
        self.assertEqual(metadata.document_metadata(1, self.path), (category_id, [tag_id]))
        with self.assertRaises(ValueError), closing(open_database(self.path)) as conn, conn:
            conn.execute("UPDATE documents SET title='bad' WHERE id=1")
            metadata.set_document_metadata(conn, 1, None, [999])
        with closing(open_database(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT title FROM documents').fetchone()[0], 'Policy')
        metadata.delete_category(category_id, self.path)
        metadata.delete_tag(tag_id, self.path)
        self.assertEqual(metadata.document_metadata(1, self.path), (None, []))
        with closing(open_database(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0], 1)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])


if __name__ == '__main__':
    unittest.main()
