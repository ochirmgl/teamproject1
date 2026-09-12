import streamlit as st
import os
import json
from pathlib import Path
import ui
import metadata
import management
import processing
from auth import register_user, login_user
from database import init_db, open_database as connect_database
import sqlite3
from dotenv import load_dotenv
from rag_service import DocumentRAG, RAGError, resolve_document_path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "dms_system.db"
load_dotenv(BASE_DIR / ".env")

# Файл хадгалах хавтас үүсгэх
UPLOAD_FOLDER = BASE_DIR / "uploaded_files"
UPLOAD_FOLDER.mkdir(exist_ok=True)


def get_config_value(name, default=None):
    """Read AI settings from .env or Streamlit secrets."""
    value = os.getenv(name)
    if value:
        return value
    try:
        return st.secrets.get(name, default)
    except (FileNotFoundError, KeyError):
        return default


def open_database():
    """Always open the database beside app.py."""
    return connect_database(DB_PATH)


def ensure_chat_schema():
    conn = open_database()
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.execute(
        """CREATE TABLE IF NOT EXISTS chat_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT 'Шинэ чат',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
        )"""
    )
    cursor.execute(
        """CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            sources_json TEXT NOT NULL DEFAULT '[]',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES chat_sessions (id) ON DELETE CASCADE
        )"""
    )
    cursor.execute(
        """CREATE TABLE IF NOT EXISTS chat_session_documents (
            session_id INTEGER NOT NULL,
            document_id INTEGER NOT NULL,
            PRIMARY KEY (session_id, document_id),
            FOREIGN KEY (session_id) REFERENCES chat_sessions (id) ON DELETE CASCADE,
            FOREIGN KEY (document_id) REFERENCES documents (id) ON DELETE CASCADE
        )"""
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_sessions_user ON chat_sessions(user_id, updated_at)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id, id)"
    )
    conn.commit()
    conn.close()


def local_file_path(stored_path):
    """Safely resolve file paths across Windows and Linux."""
    clean_path = str(stored_path).replace("\\", "/")
    return resolve_document_path(BASE_DIR, clean_path)


# Page тохиргоо (Wide layout, icon)
st.set_page_config(page_title="Мэдлэгийн сан | e-Mongolia", page_icon="📘", layout="wide")
init_db()

# Shared desktop presentation.
ui.styles()

# Session state-үүд
if 'logged_in' not in st.session_state:
    st.session_state.logged_in = False
if 'username' not in st.session_state:
    st.session_state.username = ""
if 'role' not in st.session_state:
    st.session_state.role = ""
if 'chat_messages' not in st.session_state:
    st.session_state.chat_messages = []
if 'active_chat_session_id' not in st.session_state:
    st.session_state.active_chat_session_id = None
if 'loaded_chat_session_id' not in st.session_state:
    st.session_state.loaded_chat_session_id = None


# ==================== AI CHAT HELPERS ====================
def fetch_chat_documents():
    conn = open_database()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT id, title, file_path, file_type
           FROM documents
           WHERE LOWER(status) = 'active'
           ORDER BY title"""
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {"id": row[0], "title": row[1], "file_path": row[2], "file_type": row[3]}
        for row in rows
    ]


def make_document_signature(records):
    signature = []
    for record in records:
        path = local_file_path(record["file_path"])
        try:
            stat = path.stat()
            modified = stat.st_mtime_ns
            size = stat.st_size
        except FileNotFoundError:
            modified = 0
            size = 0
        signature.append(
            (
                int(record["id"]),
                str(record["title"]),
                str(record["file_path"]),
                str(record["file_type"] or ""),
                modified,
                size,
            )
        )
    return tuple(signature)


@st.cache_resource(show_spinner=False)
def build_rag_index(document_signature):
    records = [
        {
            "id": item[0],
            "title": item[1],
            "file_path": item[2],
            "file_type": item[3],
        }
        for item in document_signature
    ]
    return DocumentRAG(records, BASE_DIR)


def current_user_id():
    conn = open_database()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE username = ?", (st.session_state.username,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def create_chat_session(user_id):
    conn = open_database()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO chat_sessions (user_id, title) VALUES (?, ?)",
        (user_id, "Шинэ чат"),
    )
    session_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return session_id


def list_chat_sessions(user_id):
    conn = open_database()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT s.id, s.title, s.updated_at, COUNT(m.id)
           FROM chat_sessions AS s
           LEFT JOIN chat_messages AS m ON m.session_id = s.id
           WHERE s.user_id = ?
           GROUP BY s.id, s.title, s.updated_at
           ORDER BY s.updated_at DESC, s.id DESC""",
        (user_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": row[0],
            "title": row[1],
            "updated_at": row[2],
            "message_count": row[3],
        }
        for row in rows
    ]


def load_chat_messages(session_id, user_id):
    conn = open_database()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT m.role, m.content, m.sources_json
           FROM chat_messages AS m
           JOIN chat_sessions AS s ON s.id = m.session_id
           WHERE m.session_id = ? AND s.user_id = ?
           ORDER BY m.id""",
        (session_id, user_id),
    )
    rows = cursor.fetchall()
    conn.close()

    messages = []
    for role, content, sources_json in rows:
        try:
            sources = json.loads(sources_json or "[]")
        except (TypeError, json.JSONDecodeError):
            sources = []
        messages.append({"role": role, "content": content, "sources": sources})
    return messages


def rename_chat_session(session_id, user_id, new_title):
    title = " ".join(new_title.split())[:80]
    if not title:
        return False

    conn = open_database()
    cursor = conn.cursor()
    cursor.execute(
        """UPDATE chat_sessions
           SET title = ?, updated_at = CURRENT_TIMESTAMP
           WHERE id = ? AND user_id = ?""",
        (title, session_id, user_id),
    )
    renamed = cursor.rowcount == 1
    conn.commit()
    conn.close()
    return renamed


def delete_chat_session(session_id, user_id):
    conn = open_database()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM chat_sessions WHERE id = ? AND user_id = ?",
        (session_id, user_id),
    )
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def save_chat_exchange(session_id, user_id, question, answer, sources=None):
    conn = open_database()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT title FROM chat_sessions WHERE id = ? AND user_id = ?",
        (session_id, user_id),
    )
    session_row = cursor.fetchone()
    if session_row is None:
        conn.close()
        return

    cursor.execute(
        """INSERT INTO chat_messages (session_id, role, content, sources_json)
           VALUES (?, 'user', ?, '[]')""",
        (session_id, question),
    )
    cursor.execute(
        """INSERT INTO chat_messages (session_id, role, content, sources_json)
           VALUES (?, 'assistant', ?, ?)""",
        (session_id, answer, json.dumps(sources or [], ensure_ascii=False)),
    )

    if session_row[0] == "Шинэ чат":
        title = " ".join(question.split())[:60] or "Шинэ чат"
        cursor.execute(
            "UPDATE chat_sessions SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (title, session_id),
        )
    else:
        cursor.execute(
            "UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (session_id,),
        )
    conn.commit()
    conn.close()


def load_chat_document_ids(session_id, user_id):
    conn = open_database()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT csd.document_id
           FROM chat_session_documents AS csd
           JOIN chat_sessions AS s ON s.id = csd.session_id
           WHERE csd.session_id = ? AND s.user_id = ?
           ORDER BY csd.document_id""",
        (session_id, user_id),
    )
    rows = cursor.fetchall()
    conn.close()
    return [row[0] for row in rows]


def save_chat_document_ids(session_id, user_id, document_ids):
    conn = open_database()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT 1 FROM chat_sessions WHERE id = ? AND user_id = ?",
        (session_id, user_id),
    )
    if cursor.fetchone() is None:
        conn.close()
        return

    cursor.execute(
        "DELETE FROM chat_session_documents WHERE session_id = ?",
        (session_id,),
    )
    cursor.executemany(
        "INSERT INTO chat_session_documents (session_id, document_id) VALUES (?, ?)",
        [(session_id, int(document_id)) for document_id in document_ids],
    )
    conn.commit()
    conn.close()


def save_chat_activity(question, source_document_id=None):
    user_id = current_user_id()
    if user_id is None:
        return
    conn = open_database()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO search_history (user_id, search_query) VALUES (?, ?)",
        (user_id, question),
    )
    cursor.execute(
        "INSERT INTO activity_logs (user_id, document_id, action) VALUES (?, ?, ?)",
        (user_id, source_document_id, "ai_chat"),
    )
    conn.commit()
    conn.close()


def render_chat_sources(sources, key_prefix='current'):
    if not sources:
        return
    with st.expander(f"📚 Ашигласан эх сурвалж ({len(sources)})"):
        for source in sources:
            st.markdown(
                f"**[{source['number']}] {source['title']}**  \n"
                f"`{source['page_label']}` · `{source['file_name']}`"
            )
            st.caption(source["excerpt"])
            if source.get('file_path'):
                conn = open_database()
                allowed = conn.execute("""SELECT 1 FROM document_versions v JOIN documents d ON d.id=v.document_id
                    WHERE d.id=? AND d.status='active' AND v.file_path=?""",(source['document_id'],source['file_path'])).fetchone()
                conn.close()
                if allowed and st.button('Эх хуудас нээх',key=f"source_{key_prefix}_{source['number']}"):
                    import re
                    page = re.match(r'^(\d+)-р хуудас',source['page_label'])
                    view_document_dialog(source['title'],source['file_path'],'',int(page.group(1)) if page else 1)
                st.caption('Хариулт үүсэх үеийн файлын хувилбар')


def answer_system_question(question, selected_document_ids, documents):
    normalised = " ".join(question.lower().strip().split())
    selected_id_set = set(selected_document_ids)
    selected = [document for document in documents if document["id"] in selected_id_set]

    unclear_messages = {"?", "so", "ok", "okay", "за", "тэгээд", "тийм"}
    if normalised in unclear_messages or len(normalised) < 2:
        return (
            "Асуултаа арай тодорхой, бүтэн өгүүлбэрээр бичнэ үү. Жишээ нь: "
            "“Сахилгын шийтгэл ногдуулах журмыг ямар үндэслэлээр баталсан бэ?”"
        )

    greetings = {"hi", "hello", "hey", "сайн уу", "сайн байна уу"}
    if normalised in greetings:
        return (
            f"Сайн байна уу. Одоогоор сонгосон {len(selected)} баримтаас асуултад "
            "хариулахад бэлэн байна."
        )

    help_patterns = (
        "what can you do", "how can you help", "чи юу хийж чадах", "яаж ашиглах", "тусламж"
    )
    if any(pattern in normalised for pattern in help_patterns):
        return (
            "Би сонгосон PDF болон Word баримтын агуулгыг тайлбарлах, асуултад "
            "хариулах, товчлох болон ашигласан эх сурвалжийг харуулах боломжтой. "
            "Мөн файлын тоо, нэрсийг хэлж чадна."
        )

    count_patterns = (
        "how many files", "how many documents", "number of files", "хэдэн файл",
        "хэдэн баримт", "файлын тоо", "баримтын тоо"
    )
    if any(pattern in normalised for pattern in count_patterns):
        return f"Одоогоор AI чатад {len(selected)} баримт сонгогдсон байна."

    list_patterns = (
        "what files", "which files", "list files", "list documents", "файлуудын нэр",
        "баримтуудын нэр", "ямар файл", "ямар баримт", "баримтын жагсаалт"
    )
    if any(pattern in normalised for pattern in list_patterns):
        if not selected:
            return "Одоогоор AI чатад баримт сонгоогүй байна."
        names = "\n".join(
            f"{index}. {document['title']}" for index, document in enumerate(selected, start=1)
        )
        return f"AI чатад сонгосон баримтууд:\n\n{names}"

    return None


# --- ФАЙЛЫГ ШУУД ВЭБ ДЭЭР НАЙДВАРТАЙ ХАРАХ (VIEWER DIALOG) ---

@st.dialog("Баримт бичиг үзэх", width="large")
def view_document_dialog(doc_title, file_path, file_type, initial_page=1):
    from document_viewer import render_document
    st.subheader(doc_title)
    resolved_path = local_file_path(file_path)
    if not resolved_path.is_file():
        st.error("Эх файл олдсонгүй.")
        return
    st.download_button("Эх файл татах", resolved_path.read_bytes(),
                       file_name=resolved_path.name, key="viewer_original_download")
    try:
        render_document(resolved_path, BASE_DIR, initial_page)
    except Exception as exc:
        st.error(str(exc))


# --- БАРИМТЫГ ЗАСАХ БОЛОН ФАЙЛЫГ НЬ СОЛИХ ПОПАП ЦОНХ ---
@st.dialog("✏️ Баримтын мэдээлэл засах")
def edit_document_dialog(doc_id, current_title, current_desc, current_author, current_file_path):
    categories = metadata.list_categories()
    tags = metadata.list_tags()
    current_category_id, current_tag_ids = metadata.document_metadata(doc_id)
    category_names = {row[0]: row[1] for row in categories}
    tag_names = {row[0]: row[1] for row in tags}
    category_options = [None] + list(category_names)
    with st.form(key=f"modal_edit_form_{doc_id}"):
        st.markdown("<h4 style='color:#333;'>Мэдээлэл шинэчлэх</h4>", unsafe_allow_html=True)
        new_title = st.text_input("Гарчиг", value=current_title)
        new_desc = st.text_area("Тайлбар", value=current_desc if current_desc else "")
        new_author = st.text_input("Зохиогч", value=current_author if current_author else "")
        new_category_id = st.selectbox(
            "Ангилал", category_options,
            index=category_options.index(current_category_id) if current_category_id in category_names else 0,
            format_func=lambda item: category_names.get(item, "Ангилалгүй"),
        )
        new_tag_ids = st.multiselect(
            "Шошго", list(tag_names),
            default=[item for item in current_tag_ids if item in tag_names],
            format_func=lambda item: tag_names[item],
            placeholder="Нэг эсвэл хэд хэдэн шошго сонгоно уу",
        )

        current_name = Path(str(current_file_path).replace("\\", "/")).name
        st.markdown(f"<div class='doc-meta'>Одоогийн файл: <b>{current_name}</b></div>", unsafe_allow_html=True)

        uploaded_file = st.file_uploader(
            "Файлаа чирж оруулах эсвэл сонгох",
            type=["pdf", "doc", "docx", "xls", "xlsx", "txt", "csv", "png", "jpg", "jpeg", "webp"]
        )

        col_submit1, col_submit2 = st.columns(2)
        with col_submit1:
            update_btn = st.form_submit_button("💾 Хадгалах", type="primary")
        with col_submit2:
            cancel_btn = st.form_submit_button("❌ Болих")

        if update_btn:
            if not new_title.strip():
                st.warning("Гарчиг хоосон байж болохгүй.")
                st.stop()
            try:
                management.save_document(current_user_id(), BASE_DIR, new_title, new_desc, new_author,
                    new_category_id, new_tag_ids,
                    upload=(uploaded_file.name, uploaded_file.getvalue()) if uploaded_file else None,
                    document_id=doc_id, expected_path=current_file_path, path=DB_PATH)
            except (ValueError, sqlite3.Error, OSError) as exc:
                st.error(str(exc))
            else:
                process_uploaded_document(doc_id)
                st.success("Баримт амжилттай шинэчлэгдлээ!")
                st.rerun()

        if cancel_btn:
            st.rerun()


# Desktop navigation and reusable library cards.
def go_page(page):
    if st.session_state.get("page") == "library":
        st.session_state.saved_library_filters = {
            key: st.session_state[key] for key in
            ("library_search", "filter_kind", "filter_author", "filter_category", "filter_tag", "filter_order")
            if key in st.session_state
        }
    st.session_state.page = page


def start_chat():
    st.session_state.active_chat_session_id = create_chat_session(current_user_id())
    st.session_state.loaded_chat_session_id = None
    st.session_state.chat_messages = []
    st.session_state.page = "chat"


def choose_chat(session_id):
    st.session_state.active_chat_session_id = session_id
    st.session_state.loaded_chat_session_id = None
    st.session_state.page = "chat"


def logout():
    st.session_state.clear()


def reset_filters():
    st.session_state.library_search = ""
    st.session_state.filter_kind = "Бүх төрөл"
    st.session_state.filter_author = "Бүх зохиогч"
    st.session_state.filter_category = "Бүх ангилал"
    st.session_state.filter_tag = "Бүх шошго"
    st.session_state.filter_order = "Сүүлд нэмсэн"


def library(is_admin):
    for key, value in st.session_state.get("saved_library_filters", {}).items():
        if key not in st.session_state:
            st.session_state[key] = value
    ui.hero()
    search = st.text_input("Баримт хайх", placeholder="Нэр, зохиогч, ангилал эсвэл шошгоор хайх...", key="library_search", label_visibility="collapsed")
    conn = open_database()
    documents = conn.execute("""SELECT d.id, d.title, d.description, d.file_path, d.file_type,
                               d.source_author, d.upload_date, COALESCE(c.name, '')
                               FROM documents d LEFT JOIN categories c ON c.id=d.category_id
                               WHERE d.status = 'active' ORDER BY d.id DESC""").fetchall()
    processing_states = {row[0]: row[1] for row in conn.execute('SELECT document_id,status FROM document_processing')}
    document_tags = {}
    for document_id, tag_name in conn.execute("""SELECT dt.document_id, t.name FROM document_tags dt
                                                  JOIN tags t ON t.id=dt.tag_id
                                                  ORDER BY t.name COLLATE NOCASE"""):
        document_tags.setdefault(document_id, []).append(tag_name)
    conn.close()
    documents = [(*d[:5], ui.author_name(d[5]), d[6], d[7], tuple(document_tags.get(d[0], []))) for d in documents]
    kinds = ["Бүх төрөл", "PDF", "Word", "Excel", "Текст"]
    authors = ["Бүх зохиогч"] + sorted({d[5] for d in documents if d[5]})
    categories = ["Бүх ангилал"] + sorted({d[7] for d in documents if d[7]})
    tags = ["Бүх шошго"] + sorted({tag for d in documents for tag in d[8]})
    if st.session_state.get("filter_kind", kinds[0]) not in kinds:
        st.session_state.filter_kind = kinds[0]
    old_author = ui.author_name(st.session_state.get("filter_author", authors[0]))
    st.session_state.filter_author = old_author if old_author in authors else authors[0]
    if st.session_state.get("filter_category", categories[0]) not in categories:
        st.session_state.filter_category = categories[0]
    if st.session_state.get("filter_tag", tags[0]) not in tags:
        st.session_state.filter_tag = tags[0]
    st.markdown("### Баримтын сан")
    a, b, c = st.columns(3)
    with a:
        kind = st.selectbox("Файлын төрөл", kinds, key="filter_kind")
    with b:
        category = st.selectbox("Ангилал", categories, key="filter_category")
    with c:
        tag = st.selectbox("Шошго", tags, key="filter_tag")
    a, b = st.columns([2, 1.4])
    with a:
        author = st.selectbox("Зохиогч", authors, key="filter_author")
    with b:
        order = st.selectbox("Эрэмбэлэх", ["Сүүлд нэмсэн", "Эхэнд нэмсэн", "Нэрээр"], key="filter_order")
    extensions = {"PDF": {".pdf"}, "Word": {".doc", ".docx"}, "Excel": {".xls", ".xlsx"}, "Текст": {".txt", ".csv"}}
    term = search.strip().casefold()
    filtered = [d for d in documents
                if (not term or any(term in value.casefold() for value in
                                    [d[1] or "", d[5] or "", d[7] or "", *d[8]]))
                and (author == "Бүх зохиогч" or d[5] == author)
                and (category == "Бүх ангилал" or d[7] == category)
                and (tag == "Бүх шошго" or tag in d[8])
                and (kind == "Бүх төрөл" or Path(d[3]).suffix.lower() in extensions[kind])]
    if order == "Эхэнд нэмсэн":
        filtered.reverse()
    elif order == "Нэрээр":
        filtered.sort(key=lambda d: (d[1] or "").casefold())
    st.caption(f"{len(filtered)} / {len(documents)} баримт")
    if term or kind != kinds[0] or category != categories[0] or tag != tags[0] or author != authors[0] or order != "Сүүлд нэмсэн":
        st.button("Шүүлтүүр цэвэрлэх", key="clear_filters", on_click=reset_filters)
    if not filtered:
        ui.empty("Баримт олдсонгүй", "Хайлтын үг эсвэл шүүлтүүрээ өөрчилж дахин хайна уу.")
    for offset in range(0, len(filtered), 3):
        for column, doc in zip(st.columns(3), filtered[offset:offset + 3]):
            with column, st.container(border=True, key=f"document_card_{doc[0]}"):
                ui.doc_card(doc)
                st.caption("Агуулга: " + processing.LABELS.get(processing_states.get(doc[0], 'pending'), 'Хүлээгдэж байна'))
                view, more = st.columns([1.3, 1])
                with view:
                    if st.button("Нээж үзэх", key=f"view_{doc[0]}", use_container_width=True):
                        view_document_dialog(doc[1], doc[3], doc[4])
                with more:
                    with st.popover("Үйлдлүүд", use_container_width=True):
                        path = local_file_path(doc[3])
                        if path.exists():
                            st.download_button("Эх файл татах", path.read_bytes(), file_name=path.name, mime=doc[4], key=f"download_{doc[0]}", use_container_width=True)
                        else:
                            st.warning("Эх файл олдсонгүй.")
                        if is_admin:
                            if st.button("Задалсан агуулга", key=f"processing_{doc[0]}", use_container_width=True):
                                processing_dialog(doc[0])
                            if st.button("Хувилбарын түүх", key=f"versions_{doc[0]}", use_container_width=True):
                                versions_dialog(doc[0])
                            if st.button("Мэдээлэл засах", key=f"edit_{doc[0]}", use_container_width=True):
                                edit_document_dialog(doc[0], doc[1], doc[2], doc[5], doc[3])
                            st.caption("Устгасан баримт хайлт болон AI туслахад харагдахгүй.")
                            confirm = st.checkbox("Устгахыг зөвшөөрч байна", key=f"confirm_{doc[0]}")
                            if st.button("Баримт устгах", key=f"delete_{doc[0]}", disabled=not confirm, use_container_width=True):
                                try:
                                    management.change_document_status(current_user_id(), doc[0], path=DB_PATH)
                                    st.rerun()
                                except ValueError as exc:
                                    st.error(str(exc))


def upload_page():
    st.title("Шинэ баримт оруулах")
    st.caption("Баримтын мэдээллийг бөглөж, эх файлаа мэдлэгийн санд байршуулаарай.")
    categories = metadata.list_categories()
    tags = metadata.list_tags()
    category_names = {row[0]: row[1] for row in categories}
    tag_names = {row[0]: row[1] for row in tags}
    form_col, help_col = st.columns([2, 1])
    with form_col, st.form("upload_form", clear_on_submit=False):
        title = st.text_input("Баримтын гарчиг *", placeholder="Жишээ: Хөдөлмөрийн дотоод журам")
        author = st.text_input("Зохиогч / Эх сурвалж", placeholder="Байгууллага, хэлтэс эсвэл зохиогчийн нэр")
        description = st.text_area("Тайлбар", placeholder="Баримтын зорилго, агуулгыг товч тайлбарлана уу")
        category_id = st.selectbox("Ангилал", [None] + list(category_names),
                                   format_func=lambda item: category_names.get(item, "Ангилалгүй"))
        tag_ids = st.multiselect("Шошго", list(tag_names), format_func=lambda item: tag_names[item],
                                 placeholder="Нэг эсвэл хэд хэдэн шошго сонгоно уу")
        uploaded = st.file_uploader("Эх файл *", type=["pdf", "doc", "docx", "xls", "xlsx", "txt", "csv", "png", "jpg", "jpeg", "webp"])
        if st.form_submit_button("Баримтыг байршуулах", type="primary", use_container_width=True):
            if title.strip() and uploaded:
                try:
                    document_id = management.save_document(current_user_id(), BASE_DIR, title, description, author,
                        category_id, tag_ids, upload=(uploaded.name, uploaded.getvalue()), path=DB_PATH)
                except (ValueError, sqlite3.Error, OSError) as exc:
                    st.error(str(exc))
                    st.stop()
                process_uploaded_document(document_id)
                st.success("Баримт амжилттай байршлаа. Баримтын сангаас нээж үзэх боломжтой.")
            else:
                st.warning("Гарчиг болон эх файлыг заавал оруулна уу.")
    with help_col, st.container(border=True, key="upload_help"):
        st.markdown("### Баримт оруулах зөвлөмж")
        st.write("Тодорхой гарчиг, зохиогчийн нэр нь хэрэгтэй мэдээллээ хурдан олоход тусална.")
        st.divider()
        st.caption("ДЭМЖИХ ФАЙЛУУД")
        st.write("PDF · Word · Excel · Текст · Зураг")
        st.caption("Дээд хэмжээ: 50 MB. Ижил нэртэй файлууд тусдаа хадгалагдана. Текст: UTF-8.")
        st.caption("PDF, DOCX, TXT, CSV, XLSX, XLS файлын агуулгыг задална. Скан болон зурагт OCR шаардлагатай. DOC файлыг DOCX болгоно уу.")


def process_uploaded_document(document_id, force=False):
    with st.spinner("Баримтын агуулгыг боловсруулж байна..."):
        status, message, sections = processing.process_document(document_id, BASE_DIR, path=DB_PATH, force=force)
    build_rag_index.clear()
    if status not in ('ready',):
        st.warning(processing.LABELS.get(status, status) + ': ' + message)
    return status, message, sections


@st.dialog("Задалсан агуулга", width="large")
def processing_dialog(document_id):
    status, message, sections = process_uploaded_document(document_id)
    st.write(processing.LABELS.get(status, status))
    st.caption(f'{len(sections)} хэсэг. PDF: хуудас; Word: гарчиг/догол мөр; Excel: хуудас/мөр.')
    if st.button("Дахин боловсруулах", key=f"retry_processing_{document_id}"):
        process_uploaded_document(document_id, force=True)
        st.rerun()
    if sections:
        index = st.selectbox('Агуулгын хэсэг', range(len(sections)), format_func=lambda i: sections[i][0])
        st.text(sections[index][1])


def processing_page():
    st.title('Баримт боловсруулалт')
    st.caption('Файлыг зөвхөн дотоодод уншина. AI үйлчилгээний төлбөртэй хүсэлт илгээхгүй.')
    conn = open_database()
    rows = conn.execute("""SELECT d.id,d.title,COALESCE(p.status,'pending'),COALESCE(p.message,'')
        FROM documents d LEFT JOIN document_processing p ON p.document_id=d.id
        WHERE d.status='active' ORDER BY d.id""").fetchall()
    conn.close()
    if st.button('Бүх баримтыг шалгаж боловсруулах', key='process_all'):
        for document_id, *_ in rows:
            process_uploaded_document(document_id)
        st.rerun()
    for document_id, title, status, message in rows:
        with st.container(border=True):
            st.write(title)
            st.caption(processing.LABELS.get(status, status))
            if message:
                st.warning(message)
            if st.button('Агуулга шалгах', key=f'process_view_{document_id}'):
                processing_dialog(document_id)


@st.dialog("Хувилбарын түүх", width="large")
def versions_dialog(document_id):
    conn = open_database()
    rows = conn.execute("""SELECT v.id,v.file_path,v.original_name,v.created_at,
                                  v.file_path=d.file_path FROM document_versions v
                           JOIN documents d ON d.id=v.document_id
                           WHERE v.document_id=? ORDER BY v.id DESC""", (document_id,)).fetchall()
    conn.close()
    st.caption("Файлын хувилбарууд хадгалагдана. Сэргээхэд гарчиг, ангилал, шошго өөрчлөгдөхгүй.")
    for version_id, stored, name, created, current in rows:
        with st.container(border=True):
            st.write(f"{name or Path(stored).name} · {created}" + (" · Одоогийн" if current else ""))
            source = local_file_path(stored)
            if source.is_file():
                st.download_button("Хувилбар татах", source.read_bytes(), file_name=name or source.name, key=f"vd_{version_id}")
                if not current:
                    confirm = st.checkbox("Энэ хувилбарыг сэргээх", key=f"vc_{version_id}")
                    if st.button("Сэргээх", key=f"vr_{version_id}", disabled=not confirm):
                        try:
                            management.restore_version(current_user_id(), document_id, version_id, BASE_DIR, path=DB_PATH)
                            process_uploaded_document(document_id)
                            st.rerun()
                        except ValueError as exc:
                            st.error(str(exc))
            else:
                st.warning("Эх файл олдсонгүй.")


def recycle_page():
    st.title("Хогийн сав")
    st.caption("Энд байгаа баримт серверээс устгагдаагүй. Сэргээхэд баримтын сан, хайлт болон AI туслахад дахин харагдана.")
    conn = open_database()
    rows = conn.execute("SELECT id,title,file_path FROM documents WHERE status='deleted' ORDER BY id DESC").fetchall()
    conn.close()
    if not rows:
        st.info("Хогийн сав хоосон байна.")
    for document_id, title, stored in rows:
        with st.container(border=True):
            st.write(title)
            st.caption("Төлөв: Хогийн саванд · Эх файл хадгалагдсан")
            exists = local_file_path(stored).is_file()
            if not exists:
                st.warning("Эх файл олдсонгүй. Сэргээхээс өмнө файлыг нөхөн байршуулна уу.")
            if st.button("Баримт сэргээх", key=f"restore_{document_id}", disabled=not exists):
                try:
                    management.change_document_status(current_user_id(), document_id, restore=True, path=DB_PATH)
                    process_uploaded_document(document_id)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))


def users_page():
    st.title("Хэрэглэгчид")
    st.caption("Хэрэглэгчийн эрх болон нэвтрэх боломжийг удирдана. Бүртгэл, баримт болон чатын түүх устахгүй.")
    conn = open_database()
    users = conn.execute("SELECT username,role_name,status,users.created_at,users.id,role_id FROM users JOIN roles ON roles.id=users.role_id ORDER BY users.id").fetchall()
    conn.close()
    ui.users_table([u[:4] for u in users])
    for username, _, status, _, target, role in users:
        with st.expander(username), st.form(f"access_{target}"):
            new_role = st.selectbox("Эрх", [1,2], index=0 if role == 1 else 1,
                                    format_func=lambda r: "Администратор" if r == 1 else "Хэрэглэгч")
            active = st.checkbox("Идэвхтэй", value=status == 'active')
            confirm = st.checkbox("Эрхийн өөрчлөлтийг баталгаажуулах")
            if st.form_submit_button("Эрх хадгалах"):
                if not confirm:
                    st.warning("Өөрчлөлтийг баталгаажуулна уу.")
                else:
                    try:
                        management.update_user_access(current_user_id(), target, new_role,
                                                      'active' if active else 'inactive', path=DB_PATH)
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))


def ai_configuration():
    from ai_provider import AIConfig
    provider = str(get_config_value('AI_PROVIDER','gemini')).strip().lower()
    def integer_setting(name, default, maximum):
        try:
            return max(1,min(int(get_config_value(name,default)),maximum))
        except (TypeError,ValueError):
            return default
    return AIConfig(provider=provider,
        model=get_config_value('OLLAMA_MODEL','qwen3:4b') if provider == 'ollama' else get_config_value('GROQ_MODEL','openai/gpt-oss-120b') if provider == 'groq' else get_config_value('GEMINI_MODEL','gemini-2.5-flash'),
        api_key=get_config_value('GROQ_API_KEY' if provider == 'groq' else 'GEMINI_API_KEY',''),
        free_tier_confirmed=str(get_config_value('GROQ_FREE_TIER_CONFIRMED' if provider == 'groq' else 'GEMINI_FREE_TIER_CONFIRMED','false')).lower() == 'true',
        daily_limit=integer_setting('AI_DAILY_REQUEST_LIMIT',20,1000),
        max_output=integer_setting('AI_MAX_OUTPUT_TOKENS',2048,4096))


def ai_settings_page():
    st.title('AI тохиргоо ба хэрэглээ')
    config = ai_configuration()
    st.write(f'{config.provider} · {config.model}')
    st.caption(f'Өдрийн хүсэлтийн хязгаар: {config.daily_limit}. Нэг асуултад хамгийн ихдээ нэг шинэ хүсэлт.')
    st.info('Gemini / Groq ашиглах бол үнэгүй төлөвлөгөөг шалгаж баталгаажуулна. Апп түлхүүрээс billing төлөвийг тодорхойлох боломжгүй. Автоматаар төлбөртэй үйлчилгээ рүү шилжихгүй.')
    conn = open_database()
    rows = conn.execute("SELECT status,COUNT(*),SUM(input_tokens),SUM(output_tokens) FROM ai_requests WHERE created_at>=date('now') GROUP BY status").fetchall()
    conn.close()
    for status,count,inputs,outputs in rows:
        st.write(f'{status}: {count} хүсэлт · {inputs or 0} оролт / {outputs or 0} гаралт токен')
    st.caption('Хүсэлтийн өдрийг UTC цагаар тоолно. Provider-ийн албан ёсны quota үүнээс бага байж болно.')
    if not rows:
        st.info('Өнөөдөр шинэ AI хүсэлт илгээгээгүй.')


def metadata_page():
    st.title("Ангилал ба шошго")
    st.caption("Баримтуудаа нэг жигд ангилж, хайлт болон шүүлтүүрийг сайжруулна.")
    st.caption("Ангилал эсвэл шошгыг устгахад холбогдсон баримтууд хадгалагдана.")
    notice = st.session_state.pop("metadata_notice", None)
    if notice:
        st.success(notice)

    left, right = st.columns(2, gap="large")
    with left:
        st.markdown("### Ангилал")
        with st.form("create_category", clear_on_submit=True):
            name = st.text_input("Шинэ ангиллын нэр", max_chars=80)
            description = st.text_area("Тайлбар", max_chars=500)
            if st.form_submit_button("Ангилал нэмэх", type="primary", use_container_width=True):
                success, message = metadata.create_category(name, description)
                if success:
                    st.session_state.metadata_notice = message
                    st.rerun()
                st.error(message)
        categories = metadata.list_categories()
        if not categories:
            st.info("Ангилал хараахан үүсээгүй байна.")
        for category_id, current_name, current_description, count in categories:
            with st.expander(f"{current_name} · {count} баримт"):
                with st.form(f"category_{category_id}"):
                    edited_name = st.text_input("Нэр", current_name, key=f"category_name_{category_id}")
                    edited_description = st.text_area("Тайлбар", current_description, key=f"category_description_{category_id}")
                    save, remove = st.columns(2)
                    save_clicked = save.form_submit_button("Хадгалах", use_container_width=True)
                    remove_clicked = remove.form_submit_button("Устгах", use_container_width=True)
                    confirm = st.checkbox("Устгахыг зөвшөөрч байна", key=f"category_confirm_{category_id}")
                    if save_clicked:
                        success, message = metadata.update_category(category_id, edited_name, edited_description)
                        if success:
                            st.session_state.metadata_notice = message
                            st.rerun()
                        st.error(message)
                    if remove_clicked:
                        if not confirm:
                            st.warning("Эхлээд устгахыг зөвшөөрнө үү.")
                        elif metadata.delete_category(category_id):
                            st.session_state.metadata_notice = "Ангиллыг устгалаа. Баримтууд ангилалгүй хэвээр хадгалагдана."
                            st.rerun()

    with right:
        st.markdown("### Шошго")
        with st.form("create_tag", clear_on_submit=True):
            name = st.text_input("Шинэ шошгын нэр", max_chars=50)
            if st.form_submit_button("Шошго нэмэх", type="primary", use_container_width=True):
                success, message = metadata.create_tag(name)
                if success:
                    st.session_state.metadata_notice = message
                    st.rerun()
                st.error(message)
        tags = metadata.list_tags()
        if not tags:
            st.info("Шошго хараахан үүсээгүй байна.")
        for tag_id, current_name, count in tags:
            with st.expander(f"#{current_name} · {count} баримт"):
                with st.form(f"tag_{tag_id}"):
                    edited_name = st.text_input("Нэр", current_name, key=f"tag_name_{tag_id}")
                    save, remove = st.columns(2)
                    save_clicked = save.form_submit_button("Хадгалах", use_container_width=True)
                    remove_clicked = remove.form_submit_button("Устгах", use_container_width=True)
                    confirm = st.checkbox("Устгахыг зөвшөөрч байна", key=f"tag_confirm_{tag_id}")
                    if save_clicked:
                        success, message = metadata.update_tag(tag_id, edited_name)
                        if success:
                            st.session_state.metadata_notice = message
                            st.rerun()
                        st.error(message)
                    if remove_clicked:
                        if not confirm:
                            st.warning("Эхлээд устгахыг зөвшөөрнө үү.")
                        elif metadata.delete_tag(tag_id):
                            st.session_state.metadata_notice = "Шошгыг устгалаа. Баримтууд хэвээр хадгалагдана."
                            st.rerun()


if st.session_state.logged_in:
    conn = open_database()
    account = conn.execute("""SELECT r.role_name,u.status FROM users u JOIN roles r ON r.id=u.role_id
                              WHERE u.username=?""", (st.session_state.username,)).fetchone()
    conn.close()
    if not account or account[1] != 'active':
        logout()
        st.rerun()
    st.session_state.role = account[0]

if st.session_state.logged_in:
    is_admin = st.session_state.role == "Admin"
    page = st.session_state.get("page", "library")
    user_id = current_user_id()
    with st.sidebar:
        ui.brand()
        st.markdown('<div class="section-label">ҮНДСЭН ЦЭС</div>', unsafe_allow_html=True)
        navigation = [("library", "Баримтын сан", "folder_open"), ("chat", "AI туслах", "chat")]
        if is_admin:
            navigation += [("upload", "Баримт оруулах", "upload_file"), ("metadata", "Ангилал ба шошго", "label"), ("users", "Хэрэглэгчид", "group")]
            navigation += [("recycle", "Хогийн сав", "recycling")]
            navigation += [("processing", "Баримт боловсруулалт", "description")]
            navigation += [("ai_settings", "AI тохиргоо", "settings")]
        for code, label, icon in navigation:
            st.button(label, icon=f":material/{icon}:", key=f"nav_{code}", type="primary" if page == code else "secondary", use_container_width=True, on_click=go_page, args=(code,))
        sessions = list_chat_sessions(user_id) if user_id else []
        if page == "chat":
            st.markdown('<div class="section-label">ЧАТЫН ТҮҮХ</div>', unsafe_allow_html=True)
            st.button("Шинэ чат", icon=":material/add:", key="sidebar_new_chat", use_container_width=True, on_click=start_chat)
            with st.container(height=260, border=False):
                if not sessions:
                    st.caption("Таны ярианууд энд хадгалагдана.")
                for session in sessions:
                    st.button(session["title"], icon=":material/chat_bubble_outline:", key=f"history_{session['id']}", help=session["title"], use_container_width=True,
                              type="primary" if st.session_state.active_chat_session_id == session["id"] else "secondary",
                              on_click=choose_chat, args=(session["id"],))
        st.divider()
        st.toggle("Бараан харагдац", key="dark_ui", on_change=lambda: None)
        ui.styles()
        st.markdown(f'<div class="profile"><div class="avatar">{ui.text(st.session_state.username[:1].upper())}</div><strong>{ui.text(st.session_state.username)}</strong></div>', unsafe_allow_html=True)
        st.button("Гарах", icon=":material/logout:", use_container_width=True, on_click=logout)
    names = {"library": "Баримтын сан", "chat": "AI туслах", "upload": "Баримт оруулах", "users": "Хэрэглэгчид", "metadata": "Ангилал ба шошго"}
    names['recycle'] = 'Хогийн сав'
    names['processing'] = 'Баримт боловсруулалт'
    names['ai_settings'] = 'AI тохиргоо'
    if not is_admin and page not in ('library', 'chat'):
        go_page('library')
        st.rerun()
    ui.header(names.get(page, "Баримтын сан"))
    if page == "library":
        library(is_admin)
    elif page == "upload" and is_admin:
        upload_page()
    elif page == "metadata" and is_admin:
        metadata_page()
    elif page == "users" and is_admin:
        users_page()
    elif page == "recycle" and is_admin:
        recycle_page()
    elif page == "processing" and is_admin:
        processing_page()
    elif page == 'ai_settings' and is_admin:
        ai_settings_page()
    elif page == "chat":
        st.title("AI туслах")
        st.caption("Баримтаа сонгоод асуугаарай. Хариултын эх сурвалжийг нээж шалгах боломжтой.")
        if user_id is None:
            st.error("Хэрэглэгчийн бүртгэл олдсонгүй.")
            st.stop()
        if st.session_state.active_chat_session_id not in [s["id"] for s in sessions]:
            st.session_state.active_chat_session_id = sessions[0]["id"] if sessions else create_chat_session(user_id)
            st.session_state.loaded_chat_session_id = None
        sessions = list_chat_sessions(user_id)
        active_session_id = st.session_state.active_chat_session_id
        active = next(s for s in sessions if s["id"] == active_session_id)
        title_col, settings_col = st.columns([5, 1])
        title_col.markdown(f"**{ui.text(active['title'])}**")
        with settings_col, st.popover("Чатын цэс", use_container_width=True):
            new_title = st.text_input("Чатын нэр", value=active["title"], max_chars=80, key=f"rename_{active_session_id}")
            if st.button("Нэр хадгалах", use_container_width=True):
                if rename_chat_session(active_session_id, user_id, new_title):
                    st.rerun()
                else:
                    st.warning("Чатын нэр хоосон байж болохгүй.")
            confirmed = st.checkbox("Энэ чатыг бүрмөсөн устгах")
            if st.button("Чат устгах", disabled=not confirmed, use_container_width=True):
                delete_chat_session(active_session_id, user_id)
                st.session_state.active_chat_session_id = None
                st.session_state.loaded_chat_session_id = None
                st.session_state.chat_messages = []
                st.rerun()
        active_session_id = st.session_state.active_chat_session_id
        if st.session_state.loaded_chat_session_id != active_session_id:
            st.session_state.chat_messages = load_chat_messages(active_session_id, user_id)
            st.session_state.loaded_chat_session_id = active_session_id

        documents = fetch_chat_documents()
        document_map = {document["id"]: document["title"] for document in documents}
        saved_document_ids = [
            document_id
            for document_id in load_chat_document_ids(active_session_id, user_id)
            if document_id in document_map
        ]
        default_document_ids = saved_document_ids or list(document_map)

        with st.expander(
            f"📚 Асуулт асуух баримтууд ({len(default_document_ids)}/{len(document_map)})",
            expanded=False,
        ):
            st.caption(
                "Ерөнхий хайлт, харьцуулалтад бүх баримтыг ашиглаж болно. "
                "Нэг сэдвийн нарийн асуултад 1–3 баримт сонговол хариулт илүү оновчтой."
            )
            selected_ids = st.multiselect(
                "Баримт сонгох",
                options=list(document_map),
                default=default_document_ids,
                format_func=lambda document_id: document_map[document_id],
                placeholder="Нэг эсвэл хэд хэдэн баримт сонгоно уу",
                key=f"chat_documents_{active_session_id}",
                label_visibility="collapsed",
            )
        save_chat_document_ids(active_session_id, user_id, selected_ids)

        document_signature = make_document_signature(documents)
        rag_index = build_rag_index(document_signature)
        config = ai_configuration()
        provider_ready = config.provider == 'ollama' or (config.provider in ('gemini','groq') and config.api_key and config.free_tier_confirmed)

        st.caption(f"{len(selected_ids)} баримт сонгосон · Эх сурвалжтай хариулт")
        if not st.session_state.chat_messages:
            ui.empty("Баримтаасаа асуугаарай", "Сонгосон баримтын агуулгыг тайлбарлах, харьцуулах, товчлох боломжтой.")
            st.caption("Жишээ: «Энэ журмын гол нөхцөлүүд юу вэ?» · «Ажилтны үүргийг товчлооч»")
        if rag_index.errors:
            with st.expander("⚠️ Уншиж чадаагүй файл"):
                for error in rag_index.errors:
                    st.warning(error)

        if not provider_ready:
            st.warning(
                "Үнэгүй AI холболтыг тохируулж баталгаажуулна уу. Администратор AI тохиргоо хэсгийг шалгана."
            )
        elif rag_index.document_count == 0:
            st.warning("AI чатад уншигдах PDF/DOCX баримт олдсонгүй.")

        st.divider()

        for message_index, message in enumerate(st.session_state.chat_messages):
            with st.chat_message(message["role"]):
                st.markdown(message["content"])
                if message["role"] == "assistant":
                    render_chat_sources(message.get("sources", []),key_prefix=f'history_{message_index}')

        chat_disabled = not provider_ready or rag_index.document_count == 0 or not selected_ids
        prompt = st.chat_input(
            "Баримтын талаар Монгол эсвэл Англи хэлээр асууна уу...",
            disabled=chat_disabled,
        )

        if prompt:
            previous_messages = list(st.session_state.chat_messages)
            st.session_state.chat_messages.append({"role": "user", "content": prompt})
            with st.chat_message("user"):
                st.markdown(prompt)

            source_payload = []
            with st.chat_message("assistant"):
                try:
                    response = answer_system_question(prompt, selected_ids, documents)
                    if response is None:
                        with st.spinner("Баримтуудаас хариулт хайж байна..."):
                            response, sources = rag_index.answer(
                                question=prompt,
                                config=config,
                                user_id=user_id,
                                db_path=DB_PATH,
                                selected_document_ids=selected_ids,
                                conversation_history=previous_messages,
                            )
                        source_payload = [
                            {
                                "number": source.number,
                                "document_id": source.document_id,
                                "title": source.title,
                                "file_name": source.file_name,
                                "page_label": source.page_label,
                                "excerpt": source.excerpt,
                                "file_path": source.file_path,
                            }
                            for source in sources
                        ]
                    st.markdown(response)
                    render_chat_sources(source_payload)
                except RAGError as error:
                    response = str(error)
                    st.error(response)

            first_document_id = source_payload[0]["document_id"] if source_payload else None
            save_chat_activity(prompt, first_document_id)
            st.session_state.chat_messages.append(
                {"role": "assistant", "content": response, "sources": source_payload}
            )
            save_chat_exchange(
                active_session_id,
                user_id,
                prompt,
                response,
                source_payload,
            )


    ui.footer()
else:
    ui.brand()
    ui.header("Тавтай морил")
    intro, form = st.columns([1.25, 1], gap="large")
    with intro:
        st.markdown('<div class="auth-art"><div class="eyebrow">E-MONGOLIA · МЭДЛЭГИЙН САН</div><h1>Мэдлэгээ нэгтгэе.<br>Мэдээллээ<br>хялбар олъё.</h1><p>Байгууллагын баримт бичиг, мэдлэг, туршлагыг нэг дороос.</p><div class="auth-points">✓ &nbsp; Баримтаа эмх цэгцтэй хадгалах<br>✓ &nbsp; Хэрэгтэй мэдээллээ хурдан хайх<br>✓ &nbsp; AI туслахаас эх сурвалжтай хариулт авах</div>'+ui.skyline()+'</div>', unsafe_allow_html=True)
    with form:
        st.markdown("## Тавтай морил")
        st.caption("Бүртгэлээрээ нэвтэрч мэдлэгийн санг ашиглаарай.")
        login, register = st.tabs(["Нэвтрэх", "Бүртгүүлэх"])
        with login, st.form("login_form"):
            name = st.text_input("Нэвтрэх нэр", placeholder="Нэвтрэх нэрээ оруулна уу")
            password = st.text_input("Нууц үг", type="password", placeholder="Нууц үгээ оруулна уу")
            if st.form_submit_button("Нэвтрэх →", type="primary", use_container_width=True):
                if name.strip() and password:
                    success, role, message = login_user(name.strip(), password)
                    if success:
                        st.session_state.logged_in = True
                        st.session_state.username = name.strip()
                        st.session_state.role = role
                        st.session_state.page = "library"
                        st.rerun()
                    else:
                        st.error(message)
                else:
                    st.warning("Нэвтрэх нэр, нууц үгээ оруулна уу.")
        with register, st.form("register_form"):
            name = st.text_input("Шинэ нэвтрэх нэр")
            password = st.text_input("Шинэ нууц үг", type="password")
            confirm = st.text_input("Нууц үг давтах", type="password")
            if st.form_submit_button("Бүртгэл үүсгэх", type="primary", use_container_width=True):
                if not name.strip() or not password:
                    st.warning("Бүх талбарыг бөглөнө үү.")
                elif password != confirm:
                    st.warning("Нууц үгүүд таарахгүй байна.")
                else:
                    success, message = register_user(name.strip(), password)
                    (st.success if success else st.error)(message)
        st.caption("Нэвтрэхэд асуудал гарвал системийн администраторт хандана уу.")
    ui.footer()
