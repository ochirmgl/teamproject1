"""Desktop UI regression checks using a disposable database and files.

Run from this directory: python -m unittest test_ui -v
"""
import shutil
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import auth
import database
from streamlit.testing.v1 import AppTest


class DesktopUITests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        shutil.copy2(Path(__file__).with_name("app.py"), self.root / "app.py")
        for module in (auth, database):
            patcher = patch.object(module, "DB_PATH", self.root / "dms_system.db")
            patcher.start()
            self.addCleanup(patcher.stop)
        database.init_db()
        uploads = self.root / "uploaded_files"
        uploads.mkdir()
        (uploads / "policy.txt").write_text("Company policy test document.", encoding="utf-8")
        with closing(database.open_database()) as conn, conn:
            for title, author, status in [("Safety policy", "Ochir", "active"),
                                          ("Leave policy", "Team", "active"),
                                          ("Old policy", "Ochir", "deleted")]:
                conn.execute("""INSERT INTO documents(title, source_author, file_path, file_type, status)
                                VALUES (?, ?, 'uploaded_files/policy.txt', 'text/plain', ?)""",
                             (title, author, status))
        self.app = AppTest.from_file(str(self.root / "app.py"), default_timeout=30).run()

    def assert_clean(self):
        self.assertEqual(list(self.app.exception), [])

    def enter(self, role="Admin"):
        username = "admin"
        if role == "User":
            auth.register_user("reader", "test-password")
            username = "reader"
        self.app.session_state.logged_in = True
        self.app.session_state.username = username
        self.app.session_state.role = role
        self.app.session_state.page = "library"
        self.app.run()
        self.assert_clean()

    def test_login_and_library_filters(self):
        self.assert_clean()
        self.app.text_input[0].set_value("admin")
        self.app.text_input[1].set_value("admin123")
        self.app.button[0].click().run()
        self.assert_clean()
        self.assertEqual(self.app.session_state.page, "library")
        self.assertTrue(any(c.value == "2 / 2 баримт" for c in self.app.caption))
        self.app.text_input(key="library_search").set_value("oCHIR").run()
        self.assertTrue(any(c.value == "1 / 2 баримт" for c in self.app.caption))
        self.app.text_input(key="library_search").set_value("missing").run()
        self.assertTrue(any("Баримт олдсонгүй" in m.value for m in self.app.markdown))
        self.app.button(key="clear_filters").click().run()
        self.assertEqual(self.app.text_input(key="library_search").value, "")
        self.app.selectbox(key="filter_kind").select("PDF").run()
        self.assertTrue(any(c.value == "0 / 2 баримт" for c in self.app.caption))
        self.app.button(key="nav_upload").click().run()
        self.app.button(key="nav_library").click().run()
        self.assertEqual(self.app.selectbox(key="filter_kind").value, "PDF")
        self.assertNotIn("Зураг", self.app.selectbox(key="filter_kind").options)
        self.app.button(key="clear_filters").click().run()
        self.assertFalse(any(b.key == "clear_filters" for b in self.app.button))
        self.assert_clean()

    def test_admin_pages_and_upload_validation(self):
        self.enter()
        for page in ("upload", "metadata", "users", "recycle", "processing", "ai_settings", "chat", "library"):
            self.app.button(key=f"nav_{page}").click().run()
            self.assert_clean()
        self.app.button(key="nav_upload").click().run()
        next(b for b in self.app.button if b.label == "Баримтыг байршуулах").click().run()
        self.assertTrue(any("заавал" in w.value for w in self.app.warning))
        self.assert_clean()

    def test_normal_user_navigation(self):
        self.enter("User")
        keys = [b.key for b in self.app.button]
        self.assertNotIn("nav_upload", keys)
        self.assertNotIn("nav_users", keys)
        self.assertNotIn("nav_metadata", keys)
        self.assertNotIn("nav_recycle", keys)
        self.assertNotIn("nav_processing", keys)
        self.assertFalse(any(str(k).startswith("delete_") for k in keys))
        self.app.button(key="nav_chat").click().run()
        self.assert_clean()

    def test_recycle_restore_and_account_revocation(self):
        self.enter()
        self.app.checkbox(key="confirm_1").check().run()
        self.app.button(key="delete_1").click().run()
        self.assertFalse(any(b.key == 'view_1' for b in self.app.button))
        self.app.button(key='nav_recycle').click().run()
        self.app.button(key='restore_1').click().run()
        self.app.button(key='nav_library').click().run()
        self.assertTrue(any(b.key == 'view_1' for b in self.app.button))
        self.assert_clean()
        self.enter('User')
        with closing(database.open_database()) as conn, conn:
            conn.execute("UPDATE users SET status='inactive' WHERE username='reader'")
        self.app.run()
        self.assertFalse(self.app.session_state.logged_in)
        self.assert_clean()

    def test_admin_access_form_protects_last_admin(self):
        self.enter()
        self.app.button(key='nav_users').click().run()
        next(c for c in self.app.checkbox if c.label == 'Идэвхтэй').uncheck().run()
        next(c for c in self.app.checkbox if c.label == 'Эрхийн өөрчлөлтийг баталгаажуулах').check().run()
        next(b for b in self.app.button if b.label == 'Эрх хадгалах').click().run()
        self.assertTrue(any('Сүүлийн' in e.value for e in self.app.error))
        self.assert_clean()

    def test_processing_page_and_content_preview(self):
        self.enter()
        self.app.button(key='nav_processing').click().run()
        self.app.button(key='process_all').click().run()
        self.assertTrue(any(c.value == 'Бэлэн' for c in self.app.caption))
        self.app.button(key='process_view_1').click().run()
        self.assertTrue(any('Company policy test document.' in t.value for t in self.app.text))
        self.assert_clean()

    def test_word_preview_never_substitutes_extracted_text(self):
        from docx import Document
        document = Document()
        document.add_paragraph('Must stay in original layout')
        source = self.root / 'uploaded_files' / 'original.docx'
        document.save(source)
        with closing(database.open_database()) as conn, conn:
            doc_id = conn.execute("""INSERT INTO documents(title,file_path,file_type,status)
                VALUES ('Word','uploaded_files/original.docx','application/vnd.openxmlformats-officedocument.wordprocessingml.document','active')""").lastrowid
        self.enter()
        with patch('document_viewer.find_office',return_value=None):
            self.app.button(key=f'view_{doc_id}').click().run()
        self.assertTrue(any('LibreOffice' in e.value for e in self.app.error))
        self.assertFalse(any('Must stay in original layout' in m.value for m in self.app.markdown))
        self.assert_clean()

    def test_dark_theme_and_logout(self):
        self.enter()
        self.app.toggle(key="dark_ui").set_value(True).run()
        self.assert_clean()
        self.assertTrue(any("--canvas:#0d1b30" in m.value for m in self.app.markdown))
        next(b for b in self.app.button if b.label == "Гарах").click().run()
        self.assert_clean()
        self.assertFalse(self.app.session_state.logged_in)

    def test_account_and_chat_history_visibility(self):
        self.assertFalse(any('class="account"' in m.value for m in self.app.markdown))
        self.assertFalse(any("Дадлагын төсөл" in m.value for m in self.app.markdown))
        self.enter()
        self.assertFalse(any("Нэвтэрсэн:" in m.value for m in self.app.markdown))
        profile = next(m.value for m in self.app.markdown if 'class="profile"' in m.value)
        self.assertIn("admin", profile)
        self.assertNotIn("Администратор", profile)
        self.assertFalse(any(b.key == "sidebar_new_chat" for b in self.app.button))
        self.assertFalse(any("action-card" in m.value for m in self.app.markdown))
        self.app.button(key="nav_chat").click().run()
        self.assertTrue(any(b.key == "sidebar_new_chat" for b in self.app.button))
        self.app.button(key="nav_users").click().run()
        self.assertFalse(any(b.key == "sidebar_new_chat" for b in self.app.button))
        self.assertEqual(len(self.app.dataframe), 0)
        self.assertTrue(any('class="users-table"' in m.value for m in self.app.markdown))
        self.assert_clean()

    def test_author_normalization_and_table_escaping(self):
        import ui
        self.assertEqual(ui.author_name("legalinfo.mn сайт-аас"), "legalinfo.mn")
        self.assertEqual(ui.author_name("legalinfo.mn сайтаас"), "legalinfo.mn")
        self.assertEqual(ui.author_name("Ochir"), "Ochir")
        with patch.object(ui.st, "markdown") as render:
            ui.users_table([("<script>alert(1)</script>", "User", "active", "2026-09-09")])
        markup = render.call_args.args[0]
        self.assertNotIn("<script>", markup)
        self.assertIn("&lt;script&gt;", markup)

    def test_category_tag_create_assign_filter_and_remove(self):
        import metadata
        self.enter()
        self.app.button(key="nav_metadata").click().run()
        next(t for t in self.app.text_input if t.label == "Шинэ ангиллын нэр").set_value("Журам")
        next(b for b in self.app.button if b.label == "Ангилал нэмэх").click().run()
        self.assert_clean()
        category_id = metadata.list_categories()[0][0]
        next(t for t in self.app.text_input if t.label == "Шинэ шошгын нэр").set_value("Ажилтан")
        next(b for b in self.app.button if b.label == "Шошго нэмэх").click().run()
        tag_id = metadata.list_tags()[0][0]
        self.app.button(key="nav_library").click().run()
        # AppTest does not submit dialog fragments; exercise the shared transaction directly.
        with closing(database.open_database()) as conn, conn:
            metadata.set_document_metadata(conn, 1, category_id, [tag_id])
        self.app.run()
        self.assert_clean()
        self.assertEqual(metadata.document_metadata(1), (category_id, [tag_id]))
        self.app.selectbox(key="filter_category").select("Журам").run()
        self.app.selectbox(key="filter_tag").select("Ажилтан").run()
        self.assertTrue(any(c.value == "1 / 2 баримт" for c in self.app.caption))
        self.app.button(key="clear_filters").click().run()
        self.app.text_input(key="library_search").set_value("ажилтан").run()
        self.assertTrue(any(c.value == "1 / 2 баримт" for c in self.app.caption))
        metadata.delete_category(category_id)
        metadata.delete_tag(tag_id)
        self.app.button(key="clear_filters").click().run()
        self.assertTrue(any(c.value == "2 / 2 баримт" for c in self.app.caption))
        self.assertEqual(metadata.document_metadata(1), (None, []))
        self.assert_clean()


if __name__ == "__main__":
    unittest.main()
