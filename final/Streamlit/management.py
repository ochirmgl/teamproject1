"""Transactional document lifecycle and administrator access controls."""
from contextlib import closing
from pathlib import Path
from uuid import uuid4
from io import BytesIO
import mimetypes
import zipfile

from database import open_database
from metadata import set_document_metadata
from processing import invalidate


def require_admin(conn, actor):
    if not conn.execute("""SELECT 1 FROM users u JOIN roles r ON r.id=u.role_id
                           WHERE u.id=? AND u.status='active' AND r.role_name='Admin'""", (actor,)).fetchone():
        raise ValueError("Энэ үйлдлийг хийх администраторын эрхгүй байна.")


def validate_upload(name, data):
    name = str(name).replace('\\', '/').split('/')[-1]
    ext = Path(name).suffix.lower()
    if ext not in {'.pdf','.doc','.docx','.xls','.xlsx','.txt','.csv','.png','.jpg','.jpeg','.webp'}:
        raise ValueError("Дэмжигдээгүй файлын төрөл.")
    if not data or len(data) > 50 * 1024 * 1024:
        raise ValueError("Хоосон файл эсвэл 50 MB-аас том файл оруулах боломжгүй.")
    try:
        if ext in {'.txt', '.csv'}:
            data.decode('utf-8-sig')
            if b'\x00' in data:
                raise ValueError()
        elif ext == '.pdf':
            import pypdfium2 as pdfium
            with pdfium.PdfDocument(data) as pdf:
                if not len(pdf):
                    raise ValueError()
        elif ext in {'.docx', '.xlsx'}:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                if sum(i.file_size for i in archive.infolist()) > 200 * 1024 * 1024:
                    raise ValueError()
                expected = 'word/document.xml' if ext == '.docx' else 'xl/workbook.xml'
                if expected not in archive.namelist() or '[Content_Types].xml' not in archive.namelist():
                    raise ValueError()
                if archive.testzip() is not None:
                    raise ValueError()
        elif ext in {'.doc', '.xls'}:
            if not data.startswith(bytes.fromhex('D0CF11E0A1B11AE1')):
                raise ValueError()
        else:
            from PIL import Image
            with Image.open(BytesIO(data)) as img:
                expected = {'.png':'PNG','.jpg':'JPEG','.jpeg':'JPEG','.webp':'WEBP'}[ext]
                if img.format != expected:
                    raise ValueError()
                img.verify()
    except Exception as exc:
        raise ValueError("Файлын агуулга гэмтсэн эсвэл өргөтгөлтэйгээ тохирохгүй байна. Текст UTF-8 байх ёстой.") from exc
    return name, ext, mimetypes.guess_type(name)[0] or 'application/octet-stream'


def audit(conn, actor, document_id, action):
    conn.execute('INSERT INTO activity_logs(user_id,document_id,action) VALUES (?,?,?)',
                 (actor, document_id, action))


def save_document(actor, root, title, description, author, category, tags,
                  upload=None, document_id=None, expected_path=None, path=None):
    if not title.strip():
        raise ValueError("Гарчиг хоосон байж болохгүй.")
    validated = validate_upload(upload[0], upload[1]) if upload else None
    new_path = None
    with closing(open_database(path)) as conn:
        try:
            conn.execute('BEGIN IMMEDIATE')
            require_admin(conn, actor)
            if document_id is not None:
                current = conn.execute("SELECT file_path,file_type FROM documents WHERE id=? AND status='active'", (document_id,)).fetchone()
                if not current or (expected_path is not None and expected_path != current[0]):
                    raise ValueError("Баримт өөрчлөгдсөн байна. Хуудсаа шинэчлээд дахин оролдоно уу.")
                conn.execute("""INSERT INTO document_versions(document_id,file_path,file_type,created_by)
                    SELECT ?,?,?,? WHERE NOT EXISTS
                    (SELECT 1 FROM document_versions WHERE document_id=? AND file_path=?)""",
                    (document_id,*current,actor,document_id,current[0]))
            elif not upload:
                raise ValueError("Эх файлаа сонгоно уу.")
            if upload:
                name, ext, mime = validated
                folder = Path(root) / 'uploaded_files'
                folder.mkdir(exist_ok=True)
                new_path = folder / (uuid4().hex + ext)
                with new_path.open('xb') as output:
                    output.write(upload[1])
                    output.flush()
                    import os
                    os.fsync(output.fileno())
                stored = str(Path('uploaded_files') / new_path.name)
            if document_id is None:
                document_id = conn.execute("""INSERT INTO documents(title,file_path,file_type,uploaded_by)
                                              VALUES (?,?,?,?)""", (title.strip(),stored,mime,actor)).lastrowid
            conn.execute('UPDATE documents SET title=?,description=?,source_author=? WHERE id=?',
                         (title.strip(),description,author,document_id))
            set_document_metadata(conn, document_id, category, tags)
            if upload:
                conn.execute('UPDATE documents SET file_path=?,file_type=? WHERE id=?', (stored,mime,document_id))
                invalidate(conn, document_id, stored)
                conn.execute('INSERT INTO document_versions(document_id,file_path,file_type,original_name,created_by) VALUES (?,?,?,?,?)',
                             (document_id,stored,mime,name,actor))
            audit(conn, actor, document_id, 'document_saved')
            conn.commit()
            return document_id
        except Exception:
            conn.rollback()
            if new_path is not None:
                new_path.unlink(missing_ok=True)
            raise


def change_document_status(actor, document_id, restore=False, path=None):
    with closing(open_database(path)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        require_admin(conn, actor)
        result = conn.execute('UPDATE documents SET status=? WHERE id=? AND status=?',
                              ('active' if restore else 'deleted', document_id, 'deleted' if restore else 'active'))
        if not result.rowcount:
            raise ValueError("Баримтын төлөв өөрчлөгдсөн байна. Хуудсаа шинэчилнэ үү.")
        audit(conn, actor, document_id, 'document_restored' if restore else 'document_trashed')


def restore_version(actor, document_id, version_id, root, path=None):
    with closing(open_database(path)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        require_admin(conn, actor)
        version = conn.execute('SELECT file_path,file_type FROM document_versions WHERE id=? AND document_id=?', (version_id,document_id)).fetchone()
        if not version or not (Path(root) / version[0]).is_file():
            raise ValueError("Хувилбарын эх файл олдсонгүй.")
        if not conn.execute("UPDATE documents SET file_path=?,file_type=? WHERE id=? AND status='active'", (*version,document_id)).rowcount:
            raise ValueError("Эхлээд баримтыг хогийн савнаас сэргээнэ үү.")
        invalidate(conn, document_id, version[0])
        audit(conn, actor, document_id, f'version_restored:{version_id}')


def update_user_access(actor, target, role_id, status, path=None):
    if role_id not in (1,2) or status not in ('active','inactive'):
        raise ValueError("Эрх эсвэл төлөв буруу байна.")
    with closing(open_database(path)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        require_admin(conn, actor)
        old = conn.execute('SELECT role_id,status FROM users WHERE id=?', (target,)).fetchone()
        if not old:
            raise ValueError("Хэрэглэгч олдсонгүй.")
        if old == (1,'active') and (role_id != 1 or status != 'active'):
            count = conn.execute("SELECT COUNT(*) FROM users WHERE role_id=1 AND status='active'").fetchone()[0]
            if count <= 1:
                raise ValueError("Сүүлийн идэвхтэй администраторын эрхийг цуцлах боломжгүй.")
        conn.execute('UPDATE users SET role_id=?,status=? WHERE id=?', (role_id,status,target))
        audit(conn, actor, None, f'user_access:{target}:{role_id}:{status}')
