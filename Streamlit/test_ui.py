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
        self.app.session_state.logged_in = True
        self.app.session_state.username = "admin"
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
        next(b for b in self.app.button if b.label == "Бүх баримт харах →").click().run()
        self.assertEqual(self.app.text_input(key="library_search").value, "")
        self.app.selectbox(key="filter_kind").select("PDF").run()
        self.assertTrue(any(c.value == "0 / 2 баримт" for c in self.app.caption))
        self.assert_clean()

    def test_admin_pages_and_upload_validation(self):
        self.enter()
        for page in ("upload", "users", "chat", "library"):
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
        self.assertFalse(any(str(k).startswith("delete_") for k in keys))
        self.app.button(key="nav_chat").click().run()
        self.assert_clean()

    def test_dark_theme_and_logout(self):
        self.enter()
        self.app.toggle(key="dark_ui").set_value(True).run()
        self.assert_clean()
        self.assertTrue(any("--canvas:#0d1b30" in m.value for m in self.app.markdown))
        next(b for b in self.app.button if b.label == "Гарах").click().run()
        self.assert_clean()
        self.assertFalse(self.app.session_state.logged_in)


if __name__ == "__main__":
    unittest.main()
