"""Category and tag operations shared by the Streamlit pages."""
from database import open_database


def duplicate_name(conn, table, name, exclude_id=None):
    # SQLite NOCASE only folds ASCII; Python casefold also handles Mongolian Cyrillic.
    return any(row_id != exclude_id and clean_label(value).casefold() == name.casefold()
               for row_id, value in conn.execute(f"SELECT id, name FROM {table}"))


def clean_label(value, maximum=80):
    return " ".join(str(value or "").split())[:maximum]


def list_categories(path=None):
    conn = open_database(path)
    try:
        return conn.execute("""SELECT c.id, c.name, COALESCE(c.description, ''), COUNT(d.id)
                               FROM categories c LEFT JOIN documents d ON d.category_id=c.id
                               GROUP BY c.id ORDER BY c.name COLLATE NOCASE""").fetchall()
    finally:
        conn.close()


def list_tags(path=None):
    conn = open_database(path)
    try:
        return conn.execute("""SELECT t.id, t.name, COUNT(dt.document_id)
                               FROM tags t LEFT JOIN document_tags dt ON dt.tag_id=t.id
                               GROUP BY t.id ORDER BY t.name COLLATE NOCASE""").fetchall()
    finally:
        conn.close()


def create_category(name, description="", path=None):
    name = clean_label(name)
    if not name:
        return False, "Ангиллын нэрийг оруулна уу."
    conn = open_database(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if duplicate_name(conn, "categories", name):
            return False, "Ийм нэртэй ангилал аль хэдийн байна."
        conn.execute("INSERT INTO categories(name, description) VALUES (?, ?)",
                     (name, str(description or "").strip()[:500]))
        conn.commit()
        return True, "Ангилал нэмэгдлээ."
    except Exception as error:
        conn.rollback()
        if "UNIQUE" in str(error).upper():
            return False, "Ийм нэртэй ангилал аль хэдийн байна."
        raise
    finally:
        conn.close()


def create_tag(name, path=None):
    name = clean_label(name, 50)
    if not name:
        return False, "Шошгын нэрийг оруулна уу."
    conn = open_database(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if duplicate_name(conn, "tags", name):
            return False, "Ийм нэртэй шошго аль хэдийн байна."
        conn.execute("INSERT INTO tags(name) VALUES (?)", (name,))
        conn.commit()
        return True, "Шошго нэмэгдлээ."
    except Exception as error:
        conn.rollback()
        if "UNIQUE" in str(error).upper():
            return False, "Ийм нэртэй шошго аль хэдийн байна."
        raise
    finally:
        conn.close()


def update_category(category_id, name, description="", path=None):
    name = clean_label(name)
    if not name:
        return False, "Ангиллын нэр хоосон байж болохгүй."
    conn = open_database(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if duplicate_name(conn, "categories", name, category_id):
            return False, "Ийм нэртэй ангилал аль хэдийн байна."
        cursor = conn.execute("UPDATE categories SET name=?, description=? WHERE id=?",
                              (name, str(description or "").strip()[:500], category_id))
        conn.commit()
        return (True, "Ангилал шинэчлэгдлээ.") if cursor.rowcount else (False, "Ангилал олдсонгүй.")
    except Exception as error:
        conn.rollback()
        if "UNIQUE" in str(error).upper():
            return False, "Ийм нэртэй ангилал аль хэдийн байна."
        raise
    finally:
        conn.close()


def update_tag(tag_id, name, path=None):
    name = clean_label(name, 50)
    if not name:
        return False, "Шошгын нэр хоосон байж болохгүй."
    conn = open_database(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if duplicate_name(conn, "tags", name, tag_id):
            return False, "Ийм нэртэй шошго аль хэдийн байна."
        cursor = conn.execute("UPDATE tags SET name=? WHERE id=?", (name, tag_id))
        conn.commit()
        return (True, "Шошго шинэчлэгдлээ.") if cursor.rowcount else (False, "Шошго олдсонгүй.")
    except Exception as error:
        conn.rollback()
        if "UNIQUE" in str(error).upper():
            return False, "Ийм нэртэй шошго аль хэдийн байна."
        raise
    finally:
        conn.close()


def delete_category(category_id, path=None):
    conn = open_database(path)
    try:
        conn.execute("UPDATE documents SET category_id=NULL WHERE category_id=?", (category_id,))
        cursor = conn.execute("DELETE FROM categories WHERE id=?", (category_id,))
        conn.commit()
        return cursor.rowcount > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_tag(tag_id, path=None):
    conn = open_database(path)
    try:
        conn.execute("DELETE FROM document_tags WHERE tag_id=?", (tag_id,))
        cursor = conn.execute("DELETE FROM tags WHERE id=?", (tag_id,))
        conn.commit()
        return cursor.rowcount > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def document_metadata(document_id, path=None):
    conn = open_database(path)
    try:
        row = conn.execute("SELECT category_id FROM documents WHERE id=?", (document_id,)).fetchone()
        tags = [item[0] for item in conn.execute(
            "SELECT tag_id FROM document_tags WHERE document_id=? ORDER BY tag_id", (document_id,))]
        return (row[0] if row else None), tags
    finally:
        conn.close()


def set_document_metadata(conn, document_id, category_id, tag_ids):
    if not conn.execute("SELECT 1 FROM documents WHERE id=?", (document_id,)).fetchone():
        raise ValueError("Баримт олдсонгүй.")
    category_id = int(category_id) if category_id else None
    clean_tag_ids = sorted({int(tag_id) for tag_id in tag_ids})
    if category_id and not conn.execute("SELECT 1 FROM categories WHERE id=?", (category_id,)).fetchone():
        raise ValueError("Сонгосон ангилал олдсонгүй.")
    if clean_tag_ids:
        found = {row[0] for row in conn.execute(
            f"SELECT id FROM tags WHERE id IN ({','.join('?' * len(clean_tag_ids))})", clean_tag_ids)}
        if found != set(clean_tag_ids):
            raise ValueError("Сонгосон шошго олдсонгүй.")
    conn.execute("UPDATE documents SET category_id=? WHERE id=?", (category_id, document_id))
    conn.execute("DELETE FROM document_tags WHERE document_id=?", (document_id,))
    conn.executemany("INSERT INTO document_tags(document_id, tag_id) VALUES (?, ?)",
                     [(document_id, tag_id) for tag_id in clean_tag_ids])
