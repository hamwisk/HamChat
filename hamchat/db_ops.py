# hamchat/db_ops.py
# from __future__ import annotations
import os, sqlite3, json, time, hashlib, hmac, secrets, logging
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any, Iterable, Literal
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from hamchat.paths import settings_dir
from hamchat.db_init import ensure_database_ready  # reuse your creator/validator
from hamchat import db_init as _dbi  # to reach _get_or_create_db_key()

log = logging.getLogger("db.ops")

DB_FILENAME = "ham_mem.db"

Role = Literal["user", "admin"]
SenderType = Literal["user", "assistant", "system", "tool"]

# ---------- password hashing (scrypt) ----------

def _hash_password(plain: str, salt: bytes) -> bytes:
    # scrypt(N=2^14, r=8, p=1) → 32 bytes (adjustable later)
    return hashlib.scrypt(plain.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)

def _verify_password(plain: str, salt: bytes, expect_hash: bytes) -> bool:
    trial = _hash_password(plain, salt)
    return hmac.compare_digest(trial, expect_hash)

# ---------- connection handling ----------

def _data_dir() -> Path:
    # matches your project layout; data dir contains ham_mem.db (see tree listing)
    root = Path.cwd()
    d = root / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d

def init_and_open() -> Tuple[sqlite3.Connection, str]:
    """
    Ensure DB exists/validates, then return (conn, mode).
    mode ∈ {'open','secure_or_strict'} — we’ll read meta for the exact mode string next.
    """
    data_dir = _data_dir()
    # create/verify once
    rc = ensure_database_ready(data_dir)  # returns 0 on success
    if rc != 0:
        raise RuntimeError("Database could not be initialized/verified.")

    db_path = data_dir / DB_FILENAME

    # 1) try plain sqlite
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("PRAGMA integrity_check;")
        if cur.fetchone()[0] == "ok":
            # sanity check: meta should exist
            cur.execute("SELECT value FROM meta WHERE key='db_mode';")
            row = cur.fetchone()
            mode = row[0] if row else "open"
            return conn, mode
    except Exception:
        pass

    # 2) try SQLCipher (using your key-management)
    try:
        from pysqlcipher3 import dbapi2 as sqlcipher
    except Exception as e:
        raise RuntimeError("DB looks encrypted but pysqlcipher3 is not available.") from e

    key = _dbi._get_or_create_db_key(existing_only=True)  # reuse your keyring/env path
    if not key:
        raise RuntimeError("Encrypted DB but no key available in keyring/ENV.")

    conn = sqlcipher.connect(str(db_path))  # type: ignore
    cur = conn.cursor()
    # mirror your PRAGMA setup
    cur.execute(f"PRAGMA key = \"x'{key.hex()}'\";")
    cur.execute("PRAGMA cipher_page_size = 4096;")
    cur.execute("PRAGMA kdf_iter = 256000;")
    cur.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA512;")
    cur.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512;")
    try: cur.execute("PRAGMA cipher_memory_security = ON;")
    except Exception: pass

    # confirm readable and fetch exact mode from meta
    cur.execute("SELECT value FROM meta WHERE key='db_mode';")
    row = cur.fetchone()
    if not row:
        raise RuntimeError("Encrypted DB opened but meta.db_mode missing.")
    mode = row[0]
    return conn, mode

def _mode_from_cfg(cfg: dict) -> str:
    return (cfg.get("security", {}).get("mode") or "lite").lower()

def open_by_detection(data_dir: Path):
    """
    Open the database by detection (SQLite vs SQLCipher) and return (conn, db_mode).
    Source of truth is the file itself + meta.db_mode.
    """
    conn, meta_mode = init_and_open()  # this already tries sqlite → sqlcipher
    return conn, meta_mode

# ---------- tiny helpers ----------

def _now() -> int:
    return int(time.time())

def _one(c) -> Optional[Any]:
    r = c.fetchone()
    return r[0] if r else None

def _field_key(existing_only: bool = False) -> bytes:
    k = _dbi._get_or_create_field_key(existing_only=existing_only)
    if not k:
        raise RuntimeError("Field key unavailable; strict mode requires HC_KEY_FIELD or keyring.")
    return k

def encrypt_field(conn, plaintext: str) -> tuple[bytes, bytes]:
    """
    Encrypt a text field for strict mode using AES-GCM and the existing field key.
    Returns (ciphertext, nonce).
    """
    key = _field_key(existing_only=False)
    aes = AESGCM(key)
    nonce = os.urandom(12)
    ct = aes.encrypt(nonce, plaintext.encode("utf-8"), None)
    return ct, nonce

def decrypt_field(conn, ciphertext: bytes, nonce: bytes) -> str:
    """
    Decrypt a text field for strict mode using AES-GCM and the existing field key.
    Returns the plaintext string.
    """
    key = _field_key(existing_only=True)
    if not key:
        raise RuntimeError("Field key unavailable; cannot decrypt strict content.")
    aes = AESGCM(key)
    pt = aes.decrypt(nonce, ciphertext, None)
    return pt.decode("utf-8")

# ---------- users & auth ----------

def create_user(conn, *, name: str, handle: str, email: Optional[str],
                username: str, password: str, role: Role = "user") -> int:
    salt = secrets.token_bytes(16)
    pw_hash = _hash_password(password, salt)
    ts = _now()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO user_profiles(name, handle, email, created, updated) VALUES(?,?,?,?,?)",
        (name, handle, email, ts, ts),
    )
    user_id = cur.lastrowid
    cur.execute(
        "INSERT INTO user_auth(id, username, role, pw_salt, pw_hash, created, updated, last_login) "
        "VALUES(?,?,?,?,?,?,?,NULL)",
        (user_id, username, role, salt, pw_hash, ts, ts),
    )
    conn.commit()
    return user_id

def probe_admin_exists(conn) -> bool:
    return count_admins(conn) > 0

def authenticate(conn, *, username: str, password: str) -> Optional[Tuple[int, str, Dict[str, Any]]]:
    cur = conn.cursor()
    cur.execute(
        "SELECT a.id, a.role, a.pw_salt, a.pw_hash "
        "FROM user_auth a WHERE a.username=?",
        (username,),
    )
    row = cur.fetchone()
    if not row:
        return None
    user_id, role, salt, pw_hash = int(row[0]), str(row[1]), bytes(row[2]), bytes(row[3])
    if not _verify_password(password, salt, pw_hash):
        return None
    # fetch prefs (you store them in Settings; if you later move to DB, adapt here)
    prefs = {
        "theme_variant": "dark",
        "spellcheck_enabled": True,
        "locale": "en_GB",
    }
    cur.execute("UPDATE user_auth SET last_login=?, updated=? WHERE id=?", (_now(), _now(), user_id))
    conn.commit()
    return user_id, role, prefs

def set_user_role(conn, user_id: int, role: Role) -> None:
    cur = conn.cursor()
    cur.execute("UPDATE user_auth SET role=?, updated=? WHERE id=?", (role, _now(), user_id))
    conn.commit()

def delete_user(conn, user_id: int) -> None:
    # CASCADE deletes auth & convos via FK on saved_conversations? (messages reference conversations)
    cur = conn.cursor()
    cur.execute("DELETE FROM user_profiles WHERE id=?", (user_id,))
    conn.commit()

# --- Signup request queue ---

def submit_signup_request(conn, *, name: str, handle: str, username: str,
                          email: Optional[str], password: str) -> int:
    salt = secrets.token_bytes(16)
    pw_hash = _hash_password(password, salt)
    ts = _now()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO signup_requests(name, handle, username, email, pw_salt, pw_hash, created, status) "
        "VALUES(?,?,?,?,?,?,?, 'pending')",
        (name, handle, username, email, salt, pw_hash, ts),
    )
    rid = cur.lastrowid
    conn.commit()
    return rid

def list_signup_requests(conn, *, status: str = "pending", limit: int = 100):
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, handle, username, email, created, status FROM signup_requests "
        "WHERE status=? ORDER BY created ASC LIMIT ?",
        (status, limit),
    )
    rows = cur.fetchall()
    return [dict(id=r[0], name=r[1], handle=r[2], username=r[3], email=r[4], created=r[5], status=r[6]) for r in rows]

def approve_signup_request(conn, *, request_id: int, admin_user_id: int) -> int:
    # promote into real user tables atomically
    cur = conn.cursor()
    cur.execute("SELECT name, handle, username, email, pw_salt, pw_hash FROM signup_requests WHERE id=? AND status='pending'", (request_id,))
    row = cur.fetchone()
    if not row:
        raise ValueError("Request not found or not pending")
    name, handle, username, email, salt, pw_hash = row
    ts = _now()
    try:
        cur.execute("BEGIN")
        # create profile
        cur.execute("INSERT INTO user_profiles(name, handle, email, created, updated) VALUES(?,?,?,?,?)",
                    (name, handle, email, ts, ts))
        user_id = cur.lastrowid
        # create auth
        cur.execute("INSERT INTO user_auth(id, username, role, pw_salt, pw_hash, created, updated, last_login) "
                    "VALUES(?,?,?,?,?,?,?,NULL)", (user_id, username, "user", salt, pw_hash, ts, ts))
        # mark request approved
        cur.execute("UPDATE signup_requests SET status='approved', decided_by=?, decided_at=? WHERE id=?",
                    (admin_user_id, ts, request_id))
        conn.commit()
        return user_id
    except Exception:
        conn.rollback()
        raise

def reject_signup_request(conn, *, request_id: int, admin_user_id: int, note: str = "") -> None:
    cur = conn.cursor()
    cur.execute("UPDATE signup_requests SET status='rejected', decided_by=?, decided_at=?, note=? "
                "WHERE id=? AND status='pending'", (admin_user_id, _now(), note, request_id))
    conn.commit()

def count_admins(conn) -> int:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM user_auth WHERE role='admin'")
    return int(cur.fetchone()[0])

def delete_user_safe(conn, user_id: int) -> None:
    # Don’t allow deletion of the last admin.
    cur = conn.cursor()
    cur.execute("SELECT role FROM user_auth WHERE id=?", (user_id,))
    row = cur.fetchone()
    if row and row[0] == "admin" and count_admins(conn) <= 1:
        raise RuntimeError("Cannot delete the last admin.")
    cur.execute("DELETE FROM user_profiles WHERE id=?", (user_id,))
    conn.commit()

# ---------- conversations & messages ----------

def create_conversation(conn, user_id: int, title: str) -> int:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO saved_conversations(user_id, title, created) VALUES(?,?,?)",
        (user_id, title, _now()),
    )
    conv_id = cur.lastrowid
    conn.commit()
    return conv_id

def rename_conversation(conn, conversation_id: int, title: str) -> None:
    """
    Update the title of a saved conversation.
    """
    cur = conn.cursor()
    cur.execute(
        "UPDATE saved_conversations SET title = ? WHERE id = ?",
        (title, int(conversation_id)),
    )
    conn.commit()


def delete_conversation(conn, conversation_id: int) -> None:
    """
    Delete a saved conversation and all of its messages.
    """
    cur = conn.cursor()
    # Remove messages first in case FKs aren't cascading
    cur.execute("DELETE FROM messages WHERE conversation_id = ?", (int(conversation_id),))
    cur.execute("DELETE FROM saved_conversations WHERE id = ?", (int(conversation_id),))
    conn.commit()

def add_message(conn, conversation_id: int, sender_type: SenderType,
                sender_id: Optional[int], content: str,
                metadata: Optional[Dict[str, Any]] = None) -> int:
    meta_json = json.dumps(metadata or {})
    mode = read_db_mode(conn)
    cur = conn.cursor()
    if mode == "strict":
        ct, nonce = encrypt_field(conn, content)
        cur.execute(
            "INSERT INTO messages(conversation_id, sender_type, sender_id, content, content_ct, content_nonce, content_key_id, metadata, created) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (conversation_id, sender_type, sender_id, None, ct, nonce, 1, meta_json, _now()),
        )
    else:
        cur.execute(
            "INSERT INTO messages(conversation_id, sender_type, sender_id, content, content_ct, content_nonce, content_key_id, metadata, created) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (conversation_id, sender_type, sender_id, content, None, None, None, meta_json, _now()),
        )
    mid = cur.lastrowid
    conn.commit()
    return mid

def list_conversations(conn, user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        "SELECT id, title, created FROM saved_conversations WHERE user_id=? ORDER BY created DESC LIMIT ?",
        (user_id, limit),
    )
    rows = cur.fetchall()
    return [{"id": r[0], "title": r[1], "created": r[2]} for r in rows]

def list_messages(conn, conversation_id: int, limit: int = 200) -> List[Dict[str, Any]]:
    mode = read_db_mode(conn)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, sender_type, sender_id, content, content_ct, content_nonce, metadata, created "
        "FROM messages WHERE conversation_id=? ORDER BY id ASC LIMIT ?",
        (conversation_id, limit),
    )
    rows = cur.fetchall()
    out = []
    for r in rows:
        content = r[3]
        if mode == "strict":
            ct = r[4]
            nonce = r[5]
            if content:
                pass  # legacy plaintext; prefer it
            elif ct and nonce:
                try:
                    content = decrypt_field(conn, bytes(ct), bytes(nonce))
                except Exception:
                    content = ""
        out.append({
            "id": r[0], "sender_type": r[1], "sender_id": r[2],
            "content": content, "metadata": json.loads(r[6] or "{}"), "created": r[7]
        })
    return out

# ---------- tiny admin UX helpers ----------

def read_db_mode(conn) -> str:
    cur = conn.cursor()
    cur.execute("SELECT value FROM meta WHERE key='db_mode'")
    return _one(cur) or "open"

def read_schema_version(conn) -> str:
    cur = conn.cursor()
    cur.execute("SELECT value FROM meta WHERE key='schema_version'")
    return _one(cur) or "unknown"

# ---------- boot glue you’ll call from app startup ----------

def boot_database_and_admin(maybe_admin_user: Optional[Tuple[str,str]] = None) -> Tuple[sqlite3.Connection, str]:
    """
    Ensure the DB is ready, open it, and (optionally) seed a first admin.
    maybe_admin_user: (username, password) to create if no admin exists yet.
    """
    conn, mode = init_and_open()
    if maybe_admin_user:
        username, password = maybe_admin_user
        ensure_bootstrap_admin(conn, username=username, password=password)
    return conn, mode

# ---------- Storage for attachments ----------

def cas_put(db, *, sha256: str, mime: str, src_path: str) -> int:
    """
    Ensure the file is present in on-disk CAS (data/cas/<sha256>), de-dupe by sha256, and return the id from the files table.
    """
    cas_root = _data_dir() / "cas"
    cas_root.mkdir(parents=True, exist_ok=True)
    cas_path = cas_root / sha256

    raw_bytes: Optional[bytes] = None
    if not cas_path.exists():
        raw_bytes = Path(src_path).read_bytes()
        cas_path.write_bytes(raw_bytes)

    sha_blob = bytes.fromhex(sha256)
    cur = db.cursor()
    cur.execute("SELECT id FROM files WHERE sha256=?", (sha_blob,))
    row = cur.fetchone()
    if row:
        return int(row[0])

    if raw_bytes is None:
        raw_bytes = Path(src_path).read_bytes()
    size_bytes = len(raw_bytes)
    kind = "image" if mime.startswith("image/") else "other"
    original_name = Path(src_path).name

    cur.execute(
        "INSERT OR IGNORE INTO files(kind, mime, sha256, size_bytes, width, height, page_count, duration_ms, exif_json, thumb_sha256, original_name, ref_count, created) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (kind, mime, sha_blob, size_bytes, None, None, None, None, None, None, original_name, 1, _now()),
    )
    cur.execute("SELECT id FROM files WHERE sha256=?", (sha_blob,))
    row = cur.fetchone()
    if not row:
        raise RuntimeError("Failed to insert or retrieve file metadata.")
    return int(row[0])

def cas_path_for_file(db, file_id: int) -> Optional[Path]:
    """
    Given a files.id, return the filesystem path to the CAS file (data/cas/<sha256>),
    or None if not found or the file does not exist.
    """
    cur = db.cursor()
    cur.execute("SELECT sha256 FROM files WHERE id=?", (file_id,))
    row = cur.fetchone()
    if not row:
        return None
    sha_blob = row[0]
    if isinstance(sha_blob, memoryview):
        sha_blob = sha_blob.tobytes()
    sha_hex = sha_blob.hex()
    cas_root = _data_dir() / "cas"
    path = cas_root / sha_hex
    return path if path.exists() else None
