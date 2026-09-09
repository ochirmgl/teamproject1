import streamlit as st
import os
import json
from pathlib import Path
import ui
from uuid import uuid4
from auth import register_user, login_user
from database import init_db, open_database as connect_database
import sqlite3
import subprocess
import tempfile
import shutil
import pandas as pd
import pypdfium2 as pdfium
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

def render_pdf_pages(pdf_path):
    """PDF файлын бүх хуудсыг зураг болгон харуулна."""
    pdf = pdfium.PdfDocument(str(pdf_path))

    for page_idx in range(len(pdf)):
        page = pdf[page_idx]
        image = page.render(scale=2.0).to_pil()
        st.image(image, use_container_width=True)

        if page_idx < len(pdf) - 1:
            st.divider()


def find_libreoffice():
    """Windows болон Streamlit Cloud/Linux дээр LibreOffice executable хайна."""
    # Linux / Streamlit Cloud
    soffice = shutil.which("soffice")
    if soffice:
        return soffice

    # Зарим Linux орчинд libreoffice команд байдаг
    libreoffice = shutil.which("libreoffice")
    if libreoffice:
        return libreoffice

    # Windows default locations
    possible_paths = [
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    ]

    for path in possible_paths:
        if Path(path).exists():
            return path

    return None


def office_to_pdf(source_path, output_dir):
    """Word / Excel файлыг LibreOffice ашиглан PDF болгоно."""
    office_executable = find_libreoffice()

    if not office_executable:
        raise RuntimeError(
            "LibreOffice олдсонгүй. Streamlit Cloud дээр packages.txt файлд "
            "'libreoffice' нэмсэн эсэхээ шалгана уу."
        )

    result = subprocess.run(
        [
            office_executable,
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(output_dir),
            str(source_path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    expected_pdf = Path(output_dir) / f"{Path(source_path).stem}.pdf"

    if not expected_pdf.exists():
        details = (result.stderr or result.stdout or "Тодорхойгүй алдаа").strip()
        raise RuntimeError(f"PDF хөрвүүлэлт амжилтгүй боллоо: {details}")

    return expected_pdf


@st.dialog("👀 Баримт бичиг үзэх", width="large")
def view_document_dialog(doc_title, file_path, file_type):
    st.markdown(
        f"<h3 style='color:#0284c7;'>📑 {doc_title}</h3>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<div class='doc-meta'>📂 Файлын төрөл: "
        f"<b>{file_type or 'Тодорхойгүй'}</b></div>",
        unsafe_allow_html=True,
    )

    resolved_path = local_file_path(file_path)

    if not resolved_path.exists():
        st.error("Файл сервер дээр олдсонгүй.")
        return

    ext = resolved_path.suffix.lower()
    m_type = (file_type or "").lower()

    # --- 1. PDF ---
    if ext == ".pdf" or "pdf" in m_type:
        try:
            render_pdf_pages(resolved_path)
        except Exception as e:
            st.error(f"PDF файлыг уншихад алдаа гарлаа: {e}")

    # --- 2. Word (.doc / .docx) ---
    # Эх Word файлыг LibreOffice -> PDF болгон хөрвүүлээд яг хуудасны байдлаар харуулна.
    elif (
        ext in [".doc", ".docx"]
        or "wordprocessingml" in m_type
        or "msword" in m_type
    ):
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                pdf_path = office_to_pdf(resolved_path, temp_dir)
                render_pdf_pages(pdf_path)
        except Exception as e:
            st.error(f"Word файлыг харахад алдаа гарлаа: {e}")
            st.info(
                "Streamlit Cloud ашиглаж байгаа бол GitHub repository-ийн root хэсэгт "
                "packages.txt файл үүсгээд дотор нь `libreoffice` гэж бичсэн эсэхийг шалгана уу."
            )

    # --- 3. Excel (.xlsx / .xls) ---
    # Excel-ийг хүснэгт хэлбэрээр шууд харуулах ба хүсвэл яг файл шиг PDF preview харуулна.
    elif (
        ext in [".xlsx", ".xls"]
        or "spreadsheet" in m_type
        or "excel" in m_type
    ):
        preview_mode = st.radio(
            "Харах хэлбэр",
            ["📊 Хүснэгт", "📄 Файл хэлбэрээр"],
            horizontal=True,
            key=f"excel_preview_{resolved_path.name}",
        )

        if preview_mode == "📄 Файл хэлбэрээр":
            try:
                with tempfile.TemporaryDirectory() as temp_dir:
                    pdf_path = office_to_pdf(resolved_path, temp_dir)
                    render_pdf_pages(pdf_path)
            except Exception as e:
                st.error(f"Excel файлыг PDF хэлбэрээр харахад алдаа гарлаа: {e}")
                st.info(
                    "Streamlit Cloud дээр packages.txt файлд `libreoffice` "
                    "байгаа эсэхийг шалгана уу."
                )
        else:
            try:
                excel_file = pd.ExcelFile(resolved_path)
                sheet_names = excel_file.sheet_names

                selected_sheet = (
                    st.selectbox("Хүснэгтийн хуудас (Sheet):", sheet_names)
                    if len(sheet_names) > 1
                    else sheet_names[0]
                )

                df = pd.read_excel(resolved_path, sheet_name=selected_sheet)
                st.dataframe(df, use_container_width=True)
            except Exception as e:
                st.error(f"Excel файлыг уншиж чадсангүй: {e}")

    # --- 4. Зураг ---
    elif (
        ext in [".png", ".jpg", ".jpeg", ".webp"]
        or any(t in m_type for t in ["image", "png", "jpeg", "jpg"])
    ):
        try:
            st.image(str(resolved_path), use_container_width=True)
        except Exception as e:
            st.error(f"Зургийг харуулж чадсангүй: {e}")

    # --- 5. Текст / CSV ---
    elif ext in [".txt", ".csv", ".log"] or "text" in m_type:
        try:
            with resolved_path.open("r", encoding="utf-8", errors="ignore") as f:
                st.text_area("Агуулга:", f.read(), height=450)
        except Exception as e:
            st.error(f"Текст файлыг уншиж чадсангүй: {e}")

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


# Desktop navigation and reusable library cards.
def go_page(page):
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
    st.session_state.filter_order = "Сүүлд нэмсэн"


def library(is_admin):
    ui.hero()
    search = st.text_input("Баримт хайх", placeholder="Баримтын нэр, зохиогчоор хайх...", key="library_search", label_visibility="collapsed")
    conn = open_database()
    documents = conn.execute("""SELECT id, title, description, file_path, file_type,
                               source_author, upload_date FROM documents
                               WHERE status = 'active' ORDER BY id DESC""").fetchall()
    conn.close()
    left, center, right = st.columns(3)
    with left:
        ui.action_card("Баримт бичиг", f"{len(documents)} баримт · Нэг дор, эмх цэгцтэй", "▤")
        st.button("Бүх баримт харах →", on_click=reset_filters, use_container_width=True)
    with center:
        ui.action_card("AI туслах", "Баримтаас эх сурвалжтай хариулт авах", "✦", "cyan")
        st.button("AI туслах нээх →", key="open_ai_card", on_click=go_page, args=("chat",), use_container_width=True)
    with right:
        if is_admin:
            ui.action_card("Шинэ баримт", "Байгууллагын мэдлэгийн санг баяжуулах", "↥", "navy")
            st.button("Баримт оруулах →", on_click=go_page, args=("upload",), use_container_width=True)
        else:
            ui.action_card("Чатын түүх", "Өмнөх яриагаа үргэлжлүүлэх", "◷", "navy")
            st.button("Чатын түүх нээх →", on_click=go_page, args=("chat",), use_container_width=True)
    st.markdown("### Баримтын сан")
    a, b, c = st.columns([2, 2, 1.4])
    with a:
        kind = st.selectbox("Файлын төрөл", ["Бүх төрөл", "PDF", "Word", "Excel", "Текст", "Зураг"], key="filter_kind")
    with b:
        author = st.selectbox("Зохиогч", ["Бүх зохиогч"] + sorted({d[5] for d in documents if d[5]}), key="filter_author")
    with c:
        order = st.selectbox("Эрэмбэлэх", ["Сүүлд нэмсэн", "Эхэнд нэмсэн", "Нэрээр"], key="filter_order")
    extensions = {"PDF": {".pdf"}, "Word": {".doc", ".docx"}, "Excel": {".xls", ".xlsx"}, "Текст": {".txt", ".csv"}, "Зураг": {".png", ".jpg", ".jpeg", ".webp"}}
    term = search.strip().casefold()
    filtered = [d for d in documents
                if (not term or term in (d[1] or "").casefold() or term in (d[5] or "").casefold())
                and (author == "Бүх зохиогч" or d[5] == author)
                and (kind == "Бүх төрөл" or Path(d[3]).suffix.lower() in extensions[kind])]
    if order == "Эхэнд нэмсэн":
        filtered.reverse()
    elif order == "Нэрээр":
        filtered.sort(key=lambda d: (d[1] or "").casefold())
    st.caption(f"{len(filtered)} / {len(documents)} баримт")
    if not filtered:
        ui.empty("Баримт олдсонгүй", "Хайлтын үг эсвэл шүүлтүүрээ өөрчилж дахин хайна уу.")
    for offset in range(0, len(filtered), 3):
        for column, doc in zip(st.columns(3), filtered[offset:offset + 3]):
            with column, st.container(border=True):
                ui.doc_card(doc)
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
                            if st.button("Мэдээлэл засах", key=f"edit_{doc[0]}", use_container_width=True):
                                edit_document_dialog(doc[0], doc[1], doc[2], doc[5], doc[3])
                            st.caption("Устгасан баримт хайлт болон AI туслахад харагдахгүй.")
                            confirm = st.checkbox("Устгахыг зөвшөөрч байна", key=f"confirm_{doc[0]}")
                            if st.button("Баримт устгах", key=f"delete_{doc[0]}", disabled=not confirm, use_container_width=True):
                                conn = open_database()
                                conn.execute("UPDATE documents SET status='deleted' WHERE id=?", (doc[0],))
                                conn.commit()
                                conn.close()
                                st.rerun()


def upload_page():
    st.title("Шинэ баримт оруулах")
    st.caption("Баримтын мэдээллийг бөглөж, эх файлаа мэдлэгийн санд байршуулаарай.")
    form_col, help_col = st.columns([2, 1])
    with form_col, st.form("upload_form", clear_on_submit=False):
        title = st.text_input("Баримтын гарчиг *", placeholder="Жишээ: Хөдөлмөрийн дотоод журам")
        author = st.text_input("Зохиогч / Эх сурвалж", placeholder="Байгууллага, хэлтэс эсвэл зохиогчийн нэр")
        description = st.text_area("Тайлбар", placeholder="Баримтын зорилго, агуулгыг товч тайлбарлана уу")
        uploaded = st.file_uploader("Эх файл *", type=["pdf", "doc", "docx", "xls", "xlsx", "txt", "csv", "png", "jpg", "jpeg", "webp"])
        if st.form_submit_button("Баримтыг байршуулах", type="primary", use_container_width=True):
            if title.strip() and uploaded:
                # Generated storage names avoid collisions; original title stays readable.
                stored_name = f"{uuid4().hex}_{Path(uploaded.name).name}"
                path = UPLOAD_FOLDER / stored_name
                path.write_bytes(uploaded.getbuffer())
                conn = open_database()
                try:
                    conn.execute("""INSERT INTO documents(title, description, source_author, file_path, file_type, uploaded_by)
                                    VALUES (?, ?, ?, ?, ?, ?)""",
                                 (title.strip(), description, author, str(Path("uploaded_files") / stored_name), uploaded.type, current_user_id()))
                    conn.commit()
                finally:
                    conn.close()
                st.success("Баримт амжилттай байршлаа. Баримтын сангаас нээж үзэх боломжтой.")
            else:
                st.warning("Гарчиг болон эх файлыг заавал оруулна уу.")
    with help_col, st.container(border=True):
        st.markdown("### Баримт оруулах зөвлөмж")
        st.write("Тодорхой гарчиг, зохиогчийн нэр нь хэрэгтэй мэдээллээ хурдан олоход тусална.")
        st.divider()
        st.caption("ДЭМЖИХ ФАЙЛУУД")
        st.write("PDF · Word · Excel · Текст · Зураг")
        st.caption("AI туслах одоогоор тексттэй PDF, DOCX, TXT файлыг уншина. Скан болон зурагт OCR шаардлагатай.")


if st.session_state.logged_in:
    is_admin = st.session_state.role == "Admin"
    page = st.session_state.get("page", "library")
    user_id = current_user_id()
    with st.sidebar:
        ui.brand()
        st.markdown('<div class="section-label">ҮНДСЭН ЦЭС</div>', unsafe_allow_html=True)
        navigation = [("library", "Баримтын сан", "folder_open"), ("chat", "AI туслах", "auto_awesome")]
        if is_admin:
            navigation += [("upload", "Баримт оруулах", "upload_file"), ("users", "Хэрэглэгчид", "group")]
        for code, label, icon in navigation:
            st.button(label, icon=f":material/{icon}:", key=f"nav_{code}", type="primary" if page == code else "secondary", use_container_width=True, on_click=go_page, args=(code,))
        st.markdown('<div class="section-label">ЧАТЫН ТҮҮХ</div>', unsafe_allow_html=True)
        st.button("Шинэ чат", icon=":material/add:", key="sidebar_new_chat", use_container_width=True, on_click=start_chat)
        sessions = list_chat_sessions(user_id) if user_id else []
        with st.container(height=260, border=False):
            if not sessions:
                st.caption("Таны ярианууд энд хадгалагдана.")
            for session in sessions:
                st.button(session["title"], icon=":material/chat_bubble_outline:", key=f"history_{session['id']}", help=session["title"], use_container_width=True,
                          type="primary" if page == "chat" and st.session_state.active_chat_session_id == session["id"] else "secondary",
                          on_click=choose_chat, args=(session["id"],))
        st.divider()
        st.toggle("Бараан харагдац", key="dark_ui", on_change=lambda: None)
        ui.styles()
        st.markdown(f'<div class="profile"><div class="avatar">{ui.text(st.session_state.username[:1].upper())}</div><div><strong>{ui.text(st.session_state.username)}</strong><br><small>{"Администратор" if is_admin else "Хэрэглэгч"}</small></div></div>', unsafe_allow_html=True)
        st.button("Гарах", icon=":material/logout:", use_container_width=True, on_click=logout)
    names = {"library": "Баримтын сан", "chat": "AI туслах", "upload": "Баримт оруулах", "users": "Хэрэглэгчид"}
    ui.header(names.get(page, "Баримтын сан"), st.session_state.username)
    if page == "library":
        library(is_admin)
    elif page == "upload" and is_admin:
        upload_page()
    elif page == "users" and is_admin:
        st.title("Хэрэглэгчид")
        st.caption("Мэдлэгийн санд бүртгэлтэй хэрэглэгчид болон тэдний эрх.")
        conn = open_database()
        users = conn.execute("SELECT username, role_name, status, users.created_at FROM users JOIN roles ON roles.id=users.role_id ORDER BY users.id").fetchall()
        conn.close()
        st.dataframe(pd.DataFrame(users, columns=["Нэвтрэх нэр", "Эрх", "Төлөв", "Бүртгүүлсэн огноо"]), hide_index=True, use_container_width=True)
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
        api_key = get_config_value("GEMINI_API_KEY")
        model = get_config_value("GEMINI_MODEL", "gemini-2.5-flash")

        st.caption(f"{len(selected_ids)} баримт сонгосон · Эх сурвалжтай хариулт")
        if not st.session_state.chat_messages:
            ui.empty("Баримтаасаа асуугаарай", "Сонгосон баримтын агуулгыг тайлбарлах, харьцуулах, товчлох боломжтой.")
            st.caption("Жишээ: «Энэ журмын гол нөхцөлүүд юу вэ?» · «Ажилтны үүргийг товчлооч»")
        if rag_index.errors:
            with st.expander("⚠️ Уншиж чадаагүй файл"):
                for error in rag_index.errors:
                    st.warning(error)

        if not api_key:
            st.warning(
                "AI туслахын холболт хараахан тохируулагдаагүй байна. Системийн администраторт хандана уу."
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
