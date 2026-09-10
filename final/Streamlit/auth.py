import sqlite3
import hashlib
from pathlib import Path
from database import open_database


DB_PATH = Path(__file__).resolve().parent / "dms_system.db"

def hash_password(password):
    """Нууц үгийг sha256 ашиглан хувиргаж (hash) буцаана"""
    return hashlib.sha256(password.encode()).hexdigest()

def register_user(username, password):
    """Шинэ хэрэглэгчийг өгөгдлийн санд бүртгэх функц"""
    conn = open_database(DB_PATH)
    cursor = conn.cursor()
    pwd_hash = hash_password(password)
    role_id = 2 # Автоматаар User эрхтэй
    
    try:
        cursor.execute(
            "INSERT INTO users (username, password_hash, role_id) VALUES (?, ?, ?)",
            (username, pwd_hash, role_id)
        )
        conn.commit()
        return True, "Амжилттай бүртгэгдлээ! Та одоо нэвтэрч орно уу."
    except sqlite3.IntegrityError:
        return False, "Энэ нэвтрэх нэр аль хэдийн бүртгэгдсэн байна!"
    except Exception as e:
        return False, f"Алдаа гарлаа: {e}"
    finally:
        conn.close()

def login_user(username, password):
    """Хэрэглэгчийг нэвтрүүлэх болон эрхийг нь шалгах функц"""
    conn = open_database(DB_PATH)
    cursor = conn.cursor()
    
    pwd_hash = hash_password(password)
    
    cursor.execute('''
        SELECT users.id, users.username, roles.role_name 
        FROM users 
        JOIN roles ON users.role_id = roles.id 
        WHERE users.username = ? AND users.password_hash = ? AND users.status = 'active'
    ''', (username, pwd_hash))
    
    user = cursor.fetchone()
    conn.close()
    
    if user:
        return True, user[2], f"Тавтай морил, {user[1]}! ({user[2]})"
    else:
        return False, None, "Нэвтрэх нэр эсвэл нууц үг буруу байна!"
