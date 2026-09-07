import streamlit as st
import os
import json
from pathlib import Path
from auth import register_user, login_user
import sqlite3
import base64
import pandas as pd
import pypdfium2 as pdfium
import mammoth
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
    return sqlite3.connect(DB_PATH)


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
st.set_page_config(page_title="DMS System", page_icon="📁", layout="wide")
ensure_chat_schema()

# ==========================================
# 🎨 ЗАГВАР САЙЖРУУЛАХ CUSTOM CSS
# ==========================================
st.markdown("""
    <style>
    [data-testid="stChatMessage"] {
        border: 1px solid rgba(128, 128, 128, 0.30);
        border-radius: 12px;
        padding: 8px 12px;
    }
    [data-testid="stChatMessage"] [data-testid="stMarkdownContainer"],
    [data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] p {
        max-height: none !important;
        overflow: visible !important;
        display: block !important;
        -webkit-line-clamp: unset !important;
        white-space: normal !important;
    }
    [data-testid="stSidebar"] { 
        background-color: #1e293b; 
        border-right: 1px solid #334155; 
    }
    [data-testid="stSidebar"] p, 
    [data-testid="stSidebar"] span, 
    [data-testid="stSidebar"] label { 
        color: #f8fafc !important; 
    }
    [data-testid="stSidebar"] .stRadio > label p { 
        color: #94a3b8 !important; 
        font-size: 0.9em; 
    }
    [data-testid="stSidebar"] div[role="radiogroup"] > label { 
        padding: 10px; 
        border-radius: 5px; 
        transition: 0.3s; 
        cursor: pointer; 
    }
    [data-testid="stSidebar"] div[role="radiogroup"] > label:hover { 
        background-color: #334155 !important; 
    }
    [data-testid="stSidebar"] .stButton > button {
        background-color: #ef4444 !important;
        color: white !important;
        border: none !important;
    }
    [data-testid="stSidebar"] .stButton > button p {
        color: white !important;
        font-weight: bold;
    }
    [data-testid="stSidebar"] .stButton > button:hover {
        background-color: #dc2626 !important;
    }
    .doc-meta {
        background-color: rgba(2, 132, 199, 0.10);
        color: inherit;
        padding: 10px 15px;
        border-radius: 8px;
        font-size: 0.9em;
        margin-top: 5px;
        margin-bottom: 15px;
        border-left: 4px solid #0284c7;
    }
    .doc-desc { color: inherit; font-size: 0.95em; margin-bottom: 10px; }
    </style>
""", unsafe_allow_html=True)

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


def render_chat_sources(sources):
    if not sources:
        return
    with st.expander(f"📚 Ашигласан эх сурвалж ({len(sources)})"):
        for source in sources:
            st.markdown(
                f"**[{source['number']}] {source['title']}**  \n"
                f"`{source['page_label']}` · `{source['file_name']}`"
            )
            st.caption(source["excerpt"])


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
@st.dialog("👀 Баримт бичиг үзэх", width="large")
def view_document_dialog(doc_title, file_path, file_type):
    st.markdown(f"<h3 style='color:#0284c7;'>📑 {doc_title}</h3>", unsafe_allow_html=True)
    st.markdown(f"<div class='doc-meta'>📂 Файлын төрөл: <b>{file_type or 'Тодорхойгүй'}</b></div>", unsafe_allow_html=True)

    resolved_path = local_file_path(file_path)
    if not resolved_path.exists():
        st.error("Файл сервер дээр олдсонгүй.")
        return

    ext = resolved_path.suffix.lower()
    m_type = (file_type or "").lower()

    # --- 1. PDF (Сервер дээр хуудас бүрийг зураг болгон 100% найдвартай харуулах) ---
    if ext == ".pdf" or "pdf" in m_type:
        try:
            pdf = pdfium.PdfDocument(str(resolved_path))
            num_pages = len(pdf)
            for page_idx in range(num_pages):
                page = pdf[page_idx]
                image = page.render(scale=2).to_pil()
                st.image(image, use_container_width=True)
                if page_idx < num_pages - 1:
                    st.divider()
        except Exception as e:
            st.error(f"PDF файлыг уншихад алдаа гарлаа: {e}")

    # --- 2. Word (.docx - Зургийг таслахгүйгээр HTML болгон найдвартай харуулах) ---
    elif ext == ".docx" or "wordprocessingml" in m_type:
        try:
            with resolved_path.open("rb") as docx_file:
                result = mammoth.convert_to_html(docx_file)
                html_content = result.value
            st.markdown(
                f"""
                <div style="background-color: white; color: #111; padding: 25px; border-radius: 8px; border: 1px solid #ddd; max-height: 700px; overflow-y: auto;">
                    {html_content}
                </div>
                """,
                unsafe_allow_html=True
            )
        except Exception as e:
            st.error(f"Word файлыг уншихад алдаа гарлаа: {e}")

    # --- 3. Excel (.xlsx, .xls) ---
    elif ext in [".xlsx", ".xls"] or "spreadsheet" in m_type or "excel" in m_type:
        try:
            excel_file = pd.ExcelFile(resolved_path)
            sheet_names = excel_file.sheet_names
            selected_sheet = st.selectbox("Хүснэгтийн хуудас (Sheet):", sheet_names) if len(sheet_names) > 1 else sheet_names[0]
            df = pd.read_excel(resolved_path, sheet_name=selected_sheet)
            st.dataframe(df, use_container_width=True)
        except Exception as e:
            st.error(f"Excel файлыг уншиж чадсангүй: {e}")

    # --- 4. Зураг (PNG, JPG, JPEG, WEBP) ---
    elif ext in [".png", ".jpg", ".jpeg", ".webp"] or any(t in m_type for t in ["image", "png", "jpeg", "jpg"]):
        with resolved_path.open("rb") as img_file:
            st.image(img_file.read(), use_container_width=True)

    # --- 5. Текст / CSV ---
    elif ext in [".txt", ".csv", ".log"] or "text" in m_type:
        with resolved_path.open("r", encoding="utf-8", errors="ignore") as f:
            st.text_area("Агуулга:", f.read(), height=450)

    else:
        st.info("Энэ төрлийн файлыг харах боломжгүй байна. 'Татах' товчийг ашиглана уу.")


# --- БАРИМТЫГ ЗАСАХ БОЛОН ФАЙЛЫГ НЬ СОЛИХ ПОПАП ЦОНХ ---
@st.dialog("✏️ Баримтын мэдээлэл засах")
def edit_document_dialog(doc_id, current_title, current_desc, current_author, current_file_path):
    with st.form(key=f"modal_edit_form_{doc_id}"):
        st.markdown("<h4 style='color:#333;'>Мэдээлэл шинэчлэх</h4>", unsafe_allow_html=True)
        new_title = st.text_input("Гарчиг", value=current_title)
        new_desc = st.text_area("Тайлбар", value=current_desc if current_desc else "")
        new_author = st.text_input("Зохиогч", value=current_author if current_author else "")
        
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
            conn = open_database()
            cursor = conn.cursor()
            final_file_path = current_file_path
            final_file_type = None
            
            if uploaded_file is not None:
                current_local_path = local_file_path(current_file_path)
                if current_local_path.exists():
                    current_local_path.unlink()
                new_local_path = UPLOAD_FOLDER / uploaded_file.name
                final_file_path = str(Path("uploaded_files") / uploaded_file.name)
                with new_local_path.open("wb") as f:
                    f.write(uploaded_file.getbuffer())
                final_file_type = uploaded_file.type
                
                cursor.execute('''UPDATE documents SET title = ?, description = ?, source_author = ?, file_path = ?, file_type = ? WHERE id = ?''', (new_title, new_desc, new_author, final_file_path, final_file_type, doc_id))
            else:
                cursor.execute('''UPDATE documents SET title = ?, description = ?, source_author = ? WHERE id = ?''', (new_title, new_desc, new_author, doc_id))
                
            conn.commit()
            conn.close()
            st.success("Баримт амжилттай шинэчлэгдлээ!")
            st.rerun()
            
        if cancel_btn:
            st.rerun()

# ==========================================
# 1. ХЭРЭВ НЭВТРЭЭСЭН БАЙВАЛ
# ==========================================
if st.session_state.logged_in:
    
    # --- ХАЖУУГИЙН ЦЭС (SIDEBAR) ---
    with st.sidebar:
        st.image("https://cdn-icons-png.flaticon.com/512/3135/3135679.png", width=100)
        st.markdown(f"<h3 style='color: white; margin-bottom: 0;'>{st.session_state.username}</h3>", unsafe_allow_html=True)
        st.markdown(f"<p style='color: #94a3b8; margin-top: 0;'>Эрх: <b>{st.session_state.role}</b></p>", unsafe_allow_html=True)
        st.divider()
        
        page_selection = st.radio("ҮНДСЭН ЦЭС", ["📁 Баримт бичиг", "💬 Шинэ чат (AI)"])
        
        st.divider()
        if st.button("🚪 Системээс гарах", type="secondary", use_container_width=True):
            st.session_state.logged_in = False
            st.session_state.username = ""
            st.session_state.role = ""
            st.session_state.chat_messages = []
            st.session_state.active_chat_session_id = None
            st.session_state.loaded_chat_session_id = None
            st.session_state.pop("chat_history_selector", None)
            st.rerun()

    # ==========================================
    # ХУУДАС 1: БАРИМТ БИЧИГ (DOCUMENTS)
    # ==========================================
    if page_selection == "📁 Баримт бичиг":
        st.markdown("<h1>📁 Баримт Бичиг Удирдлагын Систем</h1>", unsafe_allow_html=True)
        
        conn = open_database()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM documents")
        total_docs = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]
        conn.close()

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("📄 Нийт Баримт", f"{total_docs} ш")
        if st.session_state.role == "Admin":
            m2.metric("👥 Нийт Хэрэглэгч", f"{total_users} хүн")
        st.divider()

        # --- АДМИН ХЭРЭГЛЭГЧ ---
        if st.session_state.role == "Admin":
            admin_tab1, admin_tab2, admin_tab3 = st.tabs(["📄 Баримтын жагсаалт", "📤 Шинэ файл оруулах", "👥 Хэрэглэгчид"])

            with admin_tab1:
                search_query = st.text_input("🔍 Баримт хайх (Гарчиг эсвэл зохиогчоор)...", placeholder="Энд бичиж хайна уу...")
                conn = open_database()
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, title, description, file_path, file_type,
                           source_author, upload_date
                    FROM documents
                    ORDER BY id DESC
                """)
                documents = cursor.fetchall()
                conn.close()

                search_term = search_query.strip().casefold()

                if search_term:
                    documents = [
                        doc for doc in documents
                        if search_term in (doc[1] or "").casefold()
                        or search_term in (doc[5] or "").casefold()
                    ]

                if documents:
                    for doc in documents:
                        doc_id = doc[0]
                        with st.container(border=True):
                            st.markdown(f"<h3 style='color:inherit; margin-bottom:5px;'>📑 {doc[1]}</h3>", unsafe_allow_html=True)
                            st.markdown(f"<div class='doc-desc'>{doc[2] if doc[2] else 'Тайлбар оруулаагүй байна...'}</div>", unsafe_allow_html=True)
                            st.markdown(f"<div class='doc-meta'>👤 <b>Зохиогч:</b> {doc[5]} &nbsp;|&nbsp; 📅 <b>Огноо:</b> {doc[6]} &nbsp;|&nbsp; 📂 <b>Төрөл:</b> {doc[4]}</div>", unsafe_allow_html=True)
                            
                            col_v, col_dl, col_del, col_edit, col_space = st.columns([1, 1.2, 1, 1, 6])
                            with col_v:
                                if st.button("👀 Үзэх", key=f"view_{doc_id}", use_container_width=True):
                                    view_document_dialog(doc[1], doc[3], doc[4])
                            with col_dl:
                                resolved_doc_path = local_file_path(doc[3])
                                if resolved_doc_path.exists():
                                    with st.popover("📥 Татах", use_container_width=True):
                                        st.write("Эх файлыг татах:")
                                        with resolved_doc_path.open("rb") as file:
                                            file_data = file.read()
                                        st.download_button(
                                            label=f"📄 {resolved_doc_path.name}",
                                            data=file_data,
                                            file_name=resolved_doc_path.name,
                                            mime=doc[4],
                                            key=f"dl_original_{doc_id}",
                                            use_container_width=True,
                                        )
                            with col_edit:
                                if st.button("✏️ Засах", key=f"edit_btn_{doc_id}", use_container_width=True):
                                    edit_document_dialog(doc_id, doc[1], doc[2], doc[5], doc[3])
                            with col_del:
                                if st.button("🗑️ Устгах", key=f"del_{doc_id}", type="primary", use_container_width=True):
                                    conn = open_database()
                                    cursor = conn.cursor()
                                    resolved_doc_path = local_file_path(doc[3])
                                    if resolved_doc_path.exists():
                                        resolved_doc_path.unlink()
                                    cursor.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
                                    conn.commit()
                                    conn.close()
                                    st.rerun()
                else:
                    st.info("Системд одоогоор баримт бүртгэгдээгүй байна.")

            with admin_tab2:
                st.markdown("<h3 style='color:#0284c7;'>📤 Шинэ баримт байршуулах</h3>", unsafe_allow_html=True)
                with st.form("admin_upload_form", clear_on_submit=True):
                    doc_title = st.text_input("Баримтын гарчиг*")
                    doc_desc = st.text_area("Тайлбар")
                    doc_author = st.text_input("Зохиогч / Эх сурвалж")
                    uploaded_file = st.file_uploader(
                        "Файлаа чирж оруулах эсвэл сонгох", 
                        type=["pdf", "doc", "docx", "xls", "xlsx", "txt", "csv", "png", "jpg", "jpeg", "webp"]
                    )
                    
                    submit_button = st.form_submit_button("Файлыг хадгалах", type="primary")
                    
                    if submit_button:
                        if doc_title and uploaded_file:
                            local_path = UPLOAD_FOLDER / uploaded_file.name
                            file_path = str(Path("uploaded_files") / uploaded_file.name)
                            with local_path.open("wb") as f:
                                f.write(uploaded_file.getbuffer())
                            
                            conn = open_database()
                            cursor = conn.cursor()
                            cursor.execute("SELECT id FROM users WHERE username = ?", (st.session_state.username,))
                            user_id = cursor.fetchone()[0]

                            cursor.execute('''INSERT INTO documents (title, description, file_path, file_type, source_author, uploaded_by) VALUES (?, ?, ?, ?, ?, ?)''', (doc_title, doc_desc, file_path, uploaded_file.type, doc_author, user_id))
                            conn.commit()
                            conn.close()
                            st.success("Файл амжилттай байршлаа!")
                            st.rerun()
                        else:
                            st.warning("Гарчиг болон файлыг заавал оруулна уу!")

            with admin_tab3:
                st.markdown("### 👥 Системийн хэрэглэгчид")
                conn = open_database()
                cursor = conn.cursor()
                cursor.execute('''SELECT users.username, roles.role_name, users.status, users.created_at FROM users JOIN roles ON users.role_id = roles.id''')
                users_list = cursor.fetchall()
                conn.close()

                for u in users_list:
                    with st.container(border=True):
                        c1, c2, c3 = st.columns(3)
                        c1.markdown(f"👤 **{u[0]}** <br> <span style='font-size:0.8em; color:gray;'>Бүртгүүлсэн: {u[3]}</span>", unsafe_allow_html=True)
                        c2.markdown(f"🛡️ **Эрх:** {u[1]}")
                        c3.markdown(f"🟢 **Төлөв:** {u[2]}")

        # --- ЭНГИЙН ХЭРЭГЛЭГЧ ---
        else:
            search_query = st.text_input("🔍 Баримт хайх (Гарчиг эсвэл зохиогчоор)...", placeholder="Хайх үгээ бичнэ үү...")
            conn = open_database()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, title, description, file_path, file_type,
                       source_author, upload_date
                FROM documents
                ORDER BY id DESC
            """)
            documents = cursor.fetchall()
            conn.close()

            search_term = search_query.strip().casefold()

            if search_term:
                documents = [
                    doc for doc in documents
                    if search_term in (doc[1] or "").casefold()
                    or search_term in (doc[5] or "").casefold()
                ]

            if documents:
                for doc in documents:
                    doc_id = doc[0]
                    with st.container(border=True):
                        st.markdown(f"<h3 style='color:inherit; margin-bottom:5px;'>📑 {doc[1]}</h3>", unsafe_allow_html=True)
                        st.markdown(f"<div class='doc-desc'>{doc[2] if doc[2] else 'Тайлбар оруулаагүй байна...'}</div>", unsafe_allow_html=True)
                        st.markdown(f"<div class='doc-meta'>👤 <b>Зохиогч:</b> {doc[5]} &nbsp;|&nbsp; 📅 <b>Огноо:</b> {doc[6]} &nbsp;|&nbsp; 📂 <b>Төрөл:</b> {doc[4]}</div>", unsafe_allow_html=True)
                        
                        col_uv, col_udl, col_uspace = st.columns([1, 1.2, 8])
                        with col_uv:
                            if st.button("👀 Үзэх", key=f"user_view_{doc_id}", use_container_width=True):
                                view_document_dialog(doc[1], doc[3], doc[4])
                        with col_udl:
                            resolved_doc_path = local_file_path(doc[3])
                            if resolved_doc_path.exists():
                                with st.popover("📥 Татах", use_container_width=True):
                                    st.write("Эх файлыг татах:")
                                    with resolved_doc_path.open("rb") as file:
                                        file_data = file.read()
                                    st.download_button(
                                        label=f"📄 {resolved_doc_path.name}",
                                        data=file_data,
                                        file_name=resolved_doc_path.name,
                                        mime=doc[4],
                                        key=f"udl_original_{doc_id}",
                                        use_container_width=True,
                                    )
            else:
                st.info("Системд одоогоор баримт бүртгэгдээгүй байна.")

    # ==========================================
    # ХУУДАС 2: ШИНЭ ЧАТ (AI CHAT)
    # ==========================================
    elif page_selection == "💬 Шинэ чат (AI)":
        st.markdown("<h1>💬 Баримт бичигтэй харилцах AI туслах</h1>", unsafe_allow_html=True)
        st.caption(
            "Монгол эсвэл Англи хэлээр асууж болно. Хариулт нь зөвхөн сонгосон PDF, "
            "Word баримтын агуулгад тулгуурлана. Баримтад байхгүй мэдээллийг систем "
            "зохиож хариулахгүй."
        )

        user_id = current_user_id()
        if user_id is None:
            st.error("Нэвтэрсэн хэрэглэгчийн мэдээлэл олдсонгүй.")
            st.stop()

        sessions = list_chat_sessions(user_id)
        session_ids = [session["id"] for session in sessions]
        if st.session_state.active_chat_session_id not in session_ids:
            if sessions:
                st.session_state.active_chat_session_id = sessions[0]["id"]
            else:
                st.session_state.active_chat_session_id = create_chat_session(user_id)
                sessions = list_chat_sessions(user_id)
            st.session_state.loaded_chat_session_id = None

        history_column, rename_column, delete_column, new_chat_column = st.columns([3, 1, 1, 1])
        
        with new_chat_column:
            st.write("")
            if st.button(
                "➕ Шинэ чат",
                type="primary",
                help="Өмнөх чатыг устгахгүйгээр шинэ хоосон чат нээнэ.",
                use_container_width=True,
            ):
                st.session_state.active_chat_session_id = create_chat_session(user_id)
                st.session_state.loaded_chat_session_id = None
                st.session_state.chat_messages = []
                st.session_state.pop("chat_history_selector", None)
                st.rerun()

        sessions = list_chat_sessions(user_id)
        session_map = {session["id"]: session for session in sessions}
        session_ids = list(session_map)
        active_session_id = st.session_state.active_chat_session_id
        active_index = session_ids.index(active_session_id)

        with rename_column:
            st.write("")
            with st.popover("✏️ Нэр солих", use_container_width=True):
                renamed_title = st.text_input(
                    "Чатын шинэ нэр",
                    value=session_map[active_session_id]["title"],
                    max_chars=80,
                    key=f"rename_chat_{active_session_id}",
                )
                if st.button(
                    "Хадгалах",
                    key=f"save_chat_name_{active_session_id}",
                    use_container_width=True,
                ):
                    if rename_chat_session(active_session_id, user_id, renamed_title):
                        st.session_state.pop("chat_history_selector", None)
                        st.rerun()
                    else:
                        st.warning("Чатын нэр хоосон байж болохгүй.")

        with delete_column:
            st.write("")
            with st.popover("🗑️ Устгах", use_container_width=True):
                st.write("Энэ чатын түүхийг бүрмөсөн устгах уу?")
                if st.button(
                    "Тийм, устгах",
                    key=f"delete_chat_{active_session_id}",
                    use_container_width=True,
                    type="primary"
                ):
                    if delete_chat_session(active_session_id, user_id):
                        st.session_state.active_chat_session_id = None
                        st.session_state.loaded_chat_session_id = None
                        st.session_state.chat_messages = []
                        st.session_state.pop("chat_history_selector", None)
                        st.rerun()

        with history_column:
            selected_session_id = st.selectbox(
                "Чатын түүх",
                options=session_ids,
                index=active_index,
                format_func=lambda session_id: (
                    f"{session_map[session_id]['title']} · "
                    f"{session_map[session_id]['updated_at'][:16]}"
                ),
                key="chat_history_selector",
            )

        if selected_session_id != active_session_id:
            st.session_state.active_chat_session_id = selected_session_id
            st.session_state.loaded_chat_session_id = None
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
        api_key = get_config_value("GEMINI_API_KEY")
        model = get_config_value("GEMINI_MODEL", "gemini-2.5-flash")

        document_column, model_column = st.columns([1, 1.4])
        document_column.metric(
            "Сонгосон баримт",
            len(selected_ids),
            help="AI туслах одоогоор эдгээр сонгосон баримтаас хариулна.",
        )
        model_column.metric(
            "AI загвар",
            model,
            help="Баримтын хэсгүүдэд тулгуурлан хариулт боловсруулах Gemini загвар.",
        )
        if rag_index.errors:
            with st.expander("⚠️ Уншиж чадаагүй файл"):
                for error in rag_index.errors:
                    st.warning(error)

        if not api_key:
            st.warning(
                "Gemini API key тохируулаагүй байна. Streamlit Cloud тохиргооны Secrets "
                "дотор `GEMINI_API_KEY` утгыг оруулна уу."
            )
        elif rag_index.document_count == 0:
            st.warning("AI чатад уншигдах PDF/DOCX баримт олдсонгүй.")

        st.divider()

        for message in st.session_state.chat_messages:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])
                if message["role"] == "assistant":
                    render_chat_sources(message.get("sources", []))

        chat_disabled = not api_key or rag_index.document_count == 0 or not selected_ids
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
                                api_key=api_key,
                                model=model,
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

# ==========================================
# 2. ХЭРЭВ НЭВТРЭЭГҮЙ БАЙВАЛ (LOGIN / REGISTER)
# ==========================================
else:
    st.markdown("<h1 style='text-align: center; color: #1e293b; margin-bottom: 30px;'>📁 Баримт Бичиг Удирдлагын Систем</h1>", unsafe_allow_html=True)
    
    col_empty1, col_center, col_empty2 = st.columns([1, 2, 1])
    
    with col_center:
        tab_login, tab_register = st.tabs(["🔐 Нэвтрэх", "📝 Бүртгүүлэх"])

        with tab_login:
            st.markdown("<h3 style='color:#0284c7;'>Системд нэвтрэх</h3>", unsafe_allow_html=True)
            login_user_input = st.text_input("Нэвтрэх нэр", key="login_user")
            login_password = st.text_input("Нууц үг", type="password", key="login_pass")
            
            if st.button("Нэвтрэх", type="primary", use_container_width=True):
                if login_user_input and login_password:
                    success, role, message = login_user(login_user_input, login_password)
                    if success:
                        st.session_state.logged_in = True
                        st.session_state.username = login_user_input
                        st.session_state.role = role
                        st.success(message)
                        st.rerun()
                    else:
                        st.error(message)
                else:
                    st.warning("Бүх талбарын бөглөнө үү!")

        with tab_register:
            st.markdown("<h3 style='color:#0284c7;'>Шинээр бүртгүүлэх</h3>", unsafe_allow_html=True)
            reg_username = st.text_input("Шинэ нэвтрэх нэр (Username)", key="reg_user")
            reg_password = st.text_input("Шинэ нууц үг (Password)", type="password", key="reg_pass")
            
            if st.button("Бүртгүүлэх", use_container_width=True):
                if reg_username and reg_password:
                    success, msg = register_user(reg_username, reg_password)
                    if success:
                        st.success(msg)
                    else:
                        st.error(msg)
                else:
                    st.warning("Бүх талбарыг бөглөнө үү!")
