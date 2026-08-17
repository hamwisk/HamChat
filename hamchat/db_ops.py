# hamchat/db_ops.py
from __future__ import annotations
import os, sqlite3, json, time, hashlib, hmac, secrets, logging, math
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any, Iterable, Literal
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from hamchat.paths import settings_dir
from hamchat.db_init import ensure_database_ready  # reuse your creator/validator
from hamchat import db_init as _dbi  # to reach _get_or_create_db_key()
from hamchat import media_helper

log = logging.getLogger("db.ops")

DB_FILENAME = "ham_mem.db"
CAS_MAGIC = b"HCAS1"

Role = Literal["user", "admin"]
SenderType = Literal["user", "assistant", "system", "tool"]
MemoryScope = Literal["user", "chat", "profile", "admin", "global"]


# ---------- password hashing (scrypt) ----------

def _hash_password(plain: str, salt: bytes) -> bytes:
    # scrypt(N=2^14, r=8, p=1) → 32 bytes (adjustable later)
    return hashlib.scrypt(plain.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)


def _verify_password(plain: str, salt: bytes, expect_hash: bytes) -> bool:
    trial = _hash_password(plain, salt)
    return hmac.compare_digest(trial, expect_hash)


# ---------- connection handling ----------

def _apply_runtime_pragmas(conn) -> None:
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON;")


def init_and_open(data_dir: Path) -> Tuple[sqlite3.Connection, str]:
    """
    Ensure DB exists/validates, then return (conn, mode).
    mode ∈ {'open','secure_or_strict'} — we’ll read meta for the exact mode string next.
    """
    data_dir = Path(data_dir)
    # create/verify once
    rc = ensure_database_ready(data_dir)  # returns 0 on success
    if rc != 0:
        raise RuntimeError("Database could not be initialized/verified.")

    db_path = data_dir / DB_FILENAME

    # 1) try plain sqlite
    try:
        conn = sqlite3.connect(db_path)
        _apply_runtime_pragmas(conn)
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
    _apply_runtime_pragmas(conn)
    cur = conn.cursor()
    # mirror your PRAGMA setup
    cur.execute(f"PRAGMA key = \"x'{key.hex()}'\";")
    cur.execute("PRAGMA cipher_page_size = 4096;")
    cur.execute("PRAGMA kdf_iter = 256000;")
    cur.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA512;")
    cur.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512;")
    try:
        cur.execute("PRAGMA cipher_memory_security = ON;")
    except Exception:
        pass

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
    conn, meta_mode = init_and_open(Path(data_dir))
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


def encrypt_bytes_for_cas(raw: bytes) -> tuple[bytes, bytes]:
    """
    Encrypt arbitrary bytes for CAS using AES-GCM and the existing field key.
    Returns (ciphertext, nonce).
    """
    key = _field_key(existing_only=False)
    aes = AESGCM(key)
    nonce = os.urandom(12)
    ct = aes.encrypt(nonce, raw, None)
    return ct, nonce


def decrypt_bytes_for_cas(ciphertext: bytes, nonce: bytes) -> bytes:
    """
    Decrypt CAS bytes using AES-GCM and the existing field key.
    Returns the raw plaintext bytes.
    """
    key = _field_key(existing_only=True)
    if not key:
        raise RuntimeError("Field key unavailable; cannot decrypt CAS content.")
    aes = AESGCM(key)
    return aes.decrypt(nonce, ciphertext, None)


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
    # Probably should remove this one and use the "safe" variant `delete_user_safe()`
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
    cur.execute(
        "SELECT name, handle, username, email, pw_salt, pw_hash FROM signup_requests WHERE id=? AND status='pending'",
        (request_id,))
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


def delete_user_safe(conn, user_id: int, *, data_dir: Path) -> None:
    """
    Safely delete a user:
      - refuse to delete the last admin
      - delete the user’s custom AI profiles (and their avatars)
      - rely on FKs to cascade conversations/messages, etc.
    """
    cur = conn.cursor()
    cur.execute("SELECT role FROM user_auth WHERE id=?", (user_id,))
    row = cur.fetchone()
    if row and row[0] == "admin" and count_admins(conn) <= 1:
        raise RuntimeError("Cannot delete the last admin.")

    # Remove this user's custom AI profiles first (built-in/admin profiles are owner_user_id NULL).
    cur.execute("SELECT id FROM ai_profiles WHERE owner_user_id=?", (int(user_id),))
    rows = cur.fetchall()
    for (pid,) in rows:
        try:
            delete_ai_profile(conn, int(pid), data_dir=Path(data_dir))
        except Exception:
            log.exception("Failed to delete AI profile %s while deleting user %s", pid, user_id)

    # Now delete the user profile (FKs handle auth, conversations, etc.)
    cur.execute("DELETE FROM user_profiles WHERE id=?", (int(user_id),))
    conn.commit()



# ---------- conversations & messages ----------

def create_conversation(conn, user_id: int, title: str, *, thinking_mode: str = "medium", use_ham_mem: bool = True) -> int:
    thinking_mode = thinking_mode if thinking_mode in {"off", "low", "medium", "high"} else "medium"
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO saved_conversations(user_id, title, created, thinking_mode, use_ham_mem) VALUES(?,?,?,?,?)",
        (user_id, title, _now(), thinking_mode, int(bool(use_ham_mem))),
    )
    conv_id = cur.lastrowid
    conn.commit()
    return conv_id


def _export_created_to_epoch(value: Any) -> Optional[int]:
    """Convert an exported chat timestamp to the schema's epoch-seconds format."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Invalid chat created timestamp.")
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        raise ValueError("Invalid chat created timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid chat created timestamp.") from exc
    return int(parsed.timestamp())


def import_chat_export(conn, *, user_id: int, payload: Dict[str, Any]) -> int:
    """Atomically import a ChatPanel JSON export for one local user."""
    if not isinstance(payload, dict):
        raise ValueError("Chat export must be a JSON object.")

    title = payload.get("title")
    messages = payload.get("messages")
    if not isinstance(title, str) or not isinstance(messages, list):
        raise ValueError("Chat export is missing a valid title or messages list.")
    created = _export_created_to_epoch(payload.get("created"))

    normalized_messages: List[Tuple[str, str, Optional[Dict[str, Any]]]] = []
    valid_roles = {"user", "assistant", "system", "tool"}
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("Chat export contains an invalid message.")
        role = message.get("role")
        text = message.get("text")
        if role not in valid_roles or not isinstance(text, str):
            raise ValueError("Chat export contains an invalid message.")
        thumbs = message.get("thumbs", [])
        if not isinstance(thumbs, list) or not all(isinstance(thumb, str) for thumb in thumbs):
            raise ValueError("Chat export contains invalid thumbnail data.")
        normalized_messages.append((role, text, {"thumbs": thumbs} if thumbs else None))

    cur = conn.cursor()
    try:
        cur.execute("BEGIN")
        cur.execute(
            "INSERT INTO saved_conversations(user_id, title, created) VALUES(?,?,?)",
            (int(user_id), title, created if created is not None else _now()),
        )
        conversation_id = int(cur.lastrowid)
        mode = read_db_mode(conn)

        for role, text, metadata in normalized_messages:
            metadata_json = json.dumps(metadata or {})
            if mode == "strict":
                ct, nonce = encrypt_field(conn, text)
                cur.execute(
                    "INSERT INTO messages(conversation_id, sender_type, sender_id, content, content_ct, content_nonce, content_key_id, metadata, created) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (conversation_id, role, None, None, ct, nonce, 1, metadata_json, _now()),
                )
            else:
                cur.execute(
                    "INSERT INTO messages(conversation_id, sender_type, sender_id, content, content_ct, content_nonce, content_key_id, metadata, created) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (conversation_id, role, None, text, None, None, None, metadata_json, _now()),
                )

        conn.commit()
        return conversation_id
    except Exception:
        conn.rollback()
        raise


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


def get_conversation_thinking_mode(conn, conversation_id: int) -> str:
    row = conn.execute(
        "SELECT thinking_mode FROM saved_conversations WHERE id=?", (int(conversation_id),)
    ).fetchone()
    mode = row[0] if row else None
    return mode if mode in {"off", "low", "medium", "high"} else "medium"


def set_conversation_thinking_mode(conn, conversation_id: int, mode: str) -> None:
    if mode not in {"off", "low", "medium", "high"}:
        raise ValueError(f"Unknown thinking mode: {mode}")
    conn.execute(
        "UPDATE saved_conversations SET thinking_mode=? WHERE id=?", (mode, int(conversation_id))
    )
    conn.commit()


def get_conversation_use_ham_mem(conn, conversation_id: int) -> bool:
    row = conn.execute("SELECT use_ham_mem FROM saved_conversations WHERE id=?", (int(conversation_id),)).fetchone()
    return True if not row else bool(row[0])


def set_conversation_use_ham_mem(conn, conversation_id: int, enabled: bool) -> None:
    conn.execute("UPDATE saved_conversations SET use_ham_mem=? WHERE id=?", (int(bool(enabled)), int(conversation_id)))
    conn.commit()


def add_message(conn, conversation_id: int, sender_type: SenderType,
                sender_id: Optional[int], content: str,
                metadata: Optional[Dict[str, Any]] = None) -> int:
    """
    Insert a message row and link any attachment files via message_files.
    Returns the new messages.id.
    """
    meta_json = json.dumps(metadata or {})
    mode = read_db_mode(conn)
    cur = conn.cursor()

    # 1) Insert message row (encrypted or plaintext depending on mode)
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

    mid = int(cur.lastrowid)

    # 2) Link attachments to files via message_files (triggers update ref_count)
    if metadata and isinstance(metadata, dict):
        attachments = metadata.get("attachments") or []
        rows_to_insert = []
        for att in attachments:
            if not isinstance(att, dict):
                continue
            fid = att.get("file_id")
            if fid is not None:
                try:
                    rows_to_insert.append((mid, int(fid), "attachment"))
                except (TypeError, ValueError):
                    pass
            thumb_fid = att.get("thumb_file_id")
            if thumb_fid is not None:
                try:
                    rows_to_insert.append((mid, int(thumb_fid), "thumb"))
                except (TypeError, ValueError):
                    pass

        if rows_to_insert:
            cur.executemany(
                "INSERT OR IGNORE INTO message_files(message_id, file_id, role) VALUES(?,?,?)",
                rows_to_insert,
            )

    conn.commit()
    return mid



def delete_conversation(conn, conversation_id: int) -> None:
    """
    Delete a saved conversation and all of its messages.
    Cascades remove messages and message_files; triggers maintain files.ref_count.
    """
    cur = conn.cursor()
    cur.execute("DELETE FROM saved_conversations WHERE id = ?", (int(conversation_id),))
    conn.commit()
    try:
        orphan_sweep(cas_sweep=True)
    except Exception:
        log.exception("orphan_sweep failed after deleting conversation %s", conversation_id)


def delete_message(conn, message_id: int) -> None:
    """
    Delete a single message. Attachment ref_counts are maintained automatically
    via message_files triggers.
    """
    cur = conn.cursor()
    cur.execute("DELETE FROM messages WHERE id = ?", (int(message_id),))
    conn.commit()


def delete_many_messages(conn, conversation_id: int, message_id: int):
    """
    Removes all messages in the conversation with id > message_id.
    """
    if not conversation_id:
        return
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM messages WHERE conversation_id = ? AND id >= ? ORDER BY id ASC",
        (int(conversation_id), int(message_id)),
    )
    rows = cur.fetchall()
    for row in rows:
        delete_message(conn, int(row[0]))

    try:
        orphan_sweep(cas_sweep=True)
    except Exception:
        log.exception("orphan_sweep failed after truncating conversation %s", conversation_id)


def orphan_sweep(cas_sweep: bool = False, mem_sweep: bool = False):
    """
    Temporary skeleton: debug-only placeholder for sweeping orphaned CAS / memory records.
    """
    summary = {"cas_deleted": 0, "vectors_deleted": 0}
    # print(f"orphan_sweep() called with cas_sweep={cas_sweep}, mem_sweep={mem_sweep}")
    return summary


def list_conversations(
    conn,
    user_id: int,
    limit: Optional[int] = 50,
    offset: int = 0,
    search: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return one newest-first page of conversation summaries.

    Only fields needed by chat-list consumers are selected.  ``id`` is used as
    a tie-breaker so pages remain deterministic when multiple chats share the
    same second-level ``created`` timestamp.
    """
    limit_value = -1 if limit is None else max(0, int(limit))
    offset = max(0, int(offset))
    where = "user_id=?"
    params: List[Any] = [int(user_id)]
    if search:
        where += " AND title LIKE ? ESCAPE '\\'"
        escaped = str(search).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
    cur = conn.cursor()
    cur.execute(
        "SELECT id, title, created FROM saved_conversations "
        f"WHERE {where} ORDER BY created DESC, id DESC LIMIT ? OFFSET ?",
        (*params, limit_value, offset),
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


# ---------- persistent memories ----------

def _memory_role(conn, owner_user_id: int) -> str:
    row = conn.execute("SELECT role FROM user_auth WHERE id=?", (int(owner_user_id),)).fetchone()
    if not row or row[0] not in {"user", "admin"}:
        raise PermissionError("Memory owner is not an active account.")
    return str(row[0])


def _validate_memory(conn, *, owner_user_id: int, scope: str,
                     conversation_id: Optional[int], profile_id: Optional[int],
                     weight: Any) -> tuple[str, Optional[int], Optional[int], float]:
    role = _memory_role(conn, owner_user_id)
    scope = str(scope or "").strip().lower()
    allowed = {"user", "chat", "profile"} if role == "user" else {"admin", "global"}
    if scope not in allowed:
        raise PermissionError("That memory scope is not available to this account.")
    if isinstance(weight, bool):
        raise ValueError("Memory weight must be a number from 0.0 to 1.0.")
    try:
        value = float(weight)
    except (TypeError, ValueError) as exc:
        raise ValueError("Memory weight must be a number from 0.0 to 1.0.") from exc
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError("Memory weight must be from 0.0 to 1.0.")
    conversation_id = int(conversation_id) if conversation_id is not None else None
    profile_id = int(profile_id) if profile_id is not None else None
    if scope == "chat":
        if conversation_id is None or profile_id is not None:
            raise ValueError("Chat memories require exactly one chat target.")
        if not conn.execute("SELECT 1 FROM saved_conversations WHERE id=? AND user_id=?", (conversation_id, owner_user_id)).fetchone():
            raise PermissionError("The selected chat is not accessible to this account.")
    elif scope == "profile":
        if profile_id is None or conversation_id is not None:
            raise ValueError("Profile memories require exactly one AI profile target.")
        if not conn.execute("SELECT 1 FROM ai_profiles WHERE id=? AND (owner_user_id=? OR is_builtin=1)", (profile_id, owner_user_id)).fetchone():
            raise PermissionError("The selected AI profile is not accessible to this account.")
    elif conversation_id is not None or profile_id is not None:
        raise ValueError("This memory scope cannot have a chat or profile target.")
    return scope, conversation_id, profile_id, value


def _memory_row_to_dict(conn, row) -> Dict[str, Any]:
    cols = ("id", "owner_user_id", "scope", "conversation_id", "profile_id", "subject", "content",
            "content_ct", "content_nonce", "content_key_id", "weight", "enabled", "created", "updated",
            "retention_until", "embedding_ref", "embedding_model", "embedding_updated")
    data = dict(zip(cols, row))
    if read_db_mode(conn) == "strict":
        try:
            data["content"] = decrypt_field(conn, bytes(data["content_ct"]), bytes(data["content_nonce"]))
        except Exception as exc:
            log.warning("Could not decrypt memory id=%s", data.get("id"))
            raise RuntimeError("Could not decrypt stored memory content.") from exc
    data.pop("content_ct", None)
    data.pop("content_nonce", None)
    data.pop("content_key_id", None)
    return data


def list_memories(conn, *, owner_user_id: int) -> List[Dict[str, Any]]:
    _memory_role(conn, owner_user_id)
    rows = conn.execute("SELECT * FROM persistent_memory WHERE owner_user_id=? ORDER BY updated DESC, id DESC", (int(owner_user_id),)).fetchall()
    return [_memory_row_to_dict(conn, row) for row in rows]


def get_memory(conn, *, owner_user_id: int, memory_id: int) -> Optional[Dict[str, Any]]:
    _memory_role(conn, owner_user_id)
    row = conn.execute("SELECT * FROM persistent_memory WHERE id=? AND owner_user_id=?", (int(memory_id), int(owner_user_id))).fetchone()
    return _memory_row_to_dict(conn, row) if row else None


def create_memory(conn, *, owner_user_id: int, content: str, scope: str,
                  conversation_id: Optional[int] = None, profile_id: Optional[int] = None,
                  weight: Any = 0.5, enabled: bool = True) -> int:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Memory content cannot be empty.")
    scope, conversation_id, profile_id, weight = _validate_memory(
        conn, owner_user_id=owner_user_id, scope=scope, conversation_id=conversation_id,
        profile_id=profile_id, weight=weight)
    ts, mode = _now(), read_db_mode(conn)
    if mode == "strict":
        ct, nonce = encrypt_field(conn, content)
        values = (owner_user_id, scope, conversation_id, profile_id, ct, nonce, 1, weight, int(bool(enabled)), ts, ts)
        sql = "INSERT INTO persistent_memory(owner_user_id,scope,conversation_id,profile_id,content,content_ct,content_nonce,content_key_id,weight,enabled,created,updated) VALUES(?,?,?,?,NULL,?,?,?,?,?,?,?)"
    else:
        values = (owner_user_id, scope, conversation_id, profile_id, content, weight, int(bool(enabled)), ts, ts)
        sql = "INSERT INTO persistent_memory(owner_user_id,scope,conversation_id,profile_id,content,weight,enabled,created,updated) VALUES(?,?,?,?,?,?,?,?,?)"
    cur = conn.cursor(); cur.execute(sql, values); conn.commit()
    return int(cur.lastrowid)


def update_memory(conn, *, owner_user_id: int, memory_id: int, content: str, scope: str,
                  conversation_id: Optional[int] = None, profile_id: Optional[int] = None,
                  weight: Any = 0.5, enabled: bool = True) -> None:
    if not get_memory(conn, owner_user_id=owner_user_id, memory_id=memory_id):
        raise PermissionError("Memory not found or not owned by this account.")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Memory content cannot be empty.")
    scope, conversation_id, profile_id, weight = _validate_memory(conn, owner_user_id=owner_user_id, scope=scope, conversation_id=conversation_id, profile_id=profile_id, weight=weight)
    ts, mode = _now(), read_db_mode(conn)
    # Derived vectors are never authoritative; invalidate before changing source content.
    conn.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (int(memory_id),))
    if mode == "strict":
        ct, nonce = encrypt_field(conn, content)
        conn.execute("UPDATE persistent_memory SET scope=?,conversation_id=?,profile_id=?,content=NULL,content_ct=?,content_nonce=?,content_key_id=1,weight=?,enabled=?,updated=? WHERE id=? AND owner_user_id=?", (scope, conversation_id, profile_id, ct, nonce, weight, int(bool(enabled)), ts, int(memory_id), int(owner_user_id)))
    else:
        conn.execute("UPDATE persistent_memory SET scope=?,conversation_id=?,profile_id=?,content=?,content_ct=NULL,content_nonce=NULL,content_key_id=NULL,weight=?,enabled=?,updated=? WHERE id=? AND owner_user_id=?", (scope, conversation_id, profile_id, content, weight, int(bool(enabled)), ts, int(memory_id), int(owner_user_id)))
    conn.commit()


def delete_memory(conn, *, owner_user_id: int, memory_id: int) -> None:
    _memory_role(conn, owner_user_id)
    cur = conn.execute("DELETE FROM persistent_memory WHERE id=? AND owner_user_id=?", (int(memory_id), int(owner_user_id)))
    conn.commit()
    if cur.rowcount != 1:
        raise PermissionError("Memory not found or not owned by this account.")


# ---------- boot glue you’ll call from app startup ----------

def boot_database_and_admin(data_dir: Path, maybe_admin_user: Optional[Tuple[str, str]] = None) -> Tuple[sqlite3.Connection, str]:
    """
    Ensure the DB is ready, open it, and (optionally) seed a first admin.
    maybe_admin_user: (username, password) to create if no admin exists yet.
    """
    conn, mode = init_and_open(Path(data_dir))
    if maybe_admin_user:
        username, password = maybe_admin_user
        ensure_bootstrap_admin(conn, username=username, password=password)
    return conn, mode


# ---------- AI profiles ----------

def _profile_row_to_dict(row) -> Dict[str, Any]:
    cols = [
        "id", "owner_user_id", "internal_name", "display_name", "short_description",
        "avatar", "system_prompt", "allowed_models", "default_model_id",
        "temperature", "top_p", "max_tokens", "is_builtin", "created", "updated",
    ]
    data = dict(zip(cols, row))
    if data.get("allowed_models"):
        try:
            data["allowed_models"] = json.loads(data["allowed_models"])
        except Exception:
            data["allowed_models"] = []
    else:
        data["allowed_models"] = None
    return data


def create_ai_profile(conn, *, owner_user_id: Optional[int], internal_name: str, display_name: str,
                      short_description: str = "", avatar: str = "", system_prompt: str = "",
                      allowed_models: Optional[list[str]] = None, default_model_id: Optional[str] = None,
                      temperature: Optional[float] = None, top_p: Optional[float] = None,
                      max_tokens: Optional[int] = None, is_builtin: bool = False) -> int:
    cur = conn.cursor()
    ts = _now()
    allowed_json = json.dumps(allowed_models) if allowed_models is not None else None
    cur.execute(
        """
        INSERT INTO ai_profiles(owner_user_id, internal_name, display_name, short_description, avatar,
                                system_prompt, allowed_models, default_model_id, temperature, top_p,
                                max_tokens, is_builtin, created, updated)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (owner_user_id, internal_name, display_name, short_description, avatar, system_prompt,
         allowed_json, default_model_id, temperature, top_p, max_tokens,
         1 if is_builtin else 0, ts, ts),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_ai_profile(conn, profile_id: int, **fields) -> None:
    if not fields:
        return
    cur = conn.cursor()
    assignments = []
    params = []
    for key, val in fields.items():
        if key == "allowed_models":
            val = json.dumps(val) if val is not None else None
        assignments.append(f"{key}=?")
        params.append(val)
    assignments.append("updated=?")
    params.append(_now())
    params.append(int(profile_id))
    cur.execute(f"UPDATE ai_profiles SET {', '.join(assignments)} WHERE id=?", params)
    conn.commit()


def get_ai_profile(conn, profile_id: int) -> Optional[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute("SELECT * FROM ai_profiles WHERE id=?", (int(profile_id),))
    row = cur.fetchone()
    if not row:
        return None
    return _profile_row_to_dict(row)


def list_ai_profiles(conn, *, owner_user_id: Optional[int], include_builtin: bool = True) -> List[Dict[str, Any]]:
    cur = conn.cursor()
    if owner_user_id is None:
        cur.execute(
            "SELECT * FROM ai_profiles ORDER BY display_name ASC, id ASC"
        )
    else:
        if include_builtin:
            cur.execute(
                "SELECT * FROM ai_profiles WHERE owner_user_id=? OR is_builtin=1 "
                "ORDER BY display_name ASC, id ASC",
                (int(owner_user_id),),
            )
        else:
            cur.execute(
                "SELECT * FROM ai_profiles WHERE owner_user_id=? "
                "ORDER BY display_name ASC, id ASC",
                (int(owner_user_id),),
            )
    rows = cur.fetchall()
    return [_profile_row_to_dict(r) for r in rows]


def delete_ai_profile(conn, profile_id: int, *, data_dir: Path) -> None:
    """
    Delete a non-default AI profile and, if it had a CAS-backed avatar that is no longer
    referenced anywhere, clean up that avatar file as well.
    """
    cur = conn.cursor()

    # Don’t allow deleting the default profile; mirror the previous behaviour.
    cur.execute("SELECT avatar FROM ai_profiles WHERE id=?", (int(profile_id),))
    row = cur.fetchone()
    avatar = row[0] if row else None

    cur.execute("DELETE FROM ai_profiles WHERE id=?", (int(profile_id),))
    conn.commit()

    # Best-effort CAS cleanup for the old avatar.
    if avatar:
        try:
            media_helper.cleanup_profile_avatar(conn, avatar, data_dir=Path(data_dir))
        except Exception:
            log.exception("Failed to clean up avatar for deleted profile %s", profile_id)



def export_ai_profile(conn, profile_id: int) -> Dict[str, Any]:
    profile = get_ai_profile(conn, profile_id)
    if not profile:
        raise ValueError("Profile not found")
    data = dict(profile)
    data["hamchat_profile_version"] = 1
    return data


def import_ai_profile(conn, *, owner_user_id: Optional[int], data: Dict[str, Any], is_builtin: bool = False) -> int:
    display_name = (data.get("display_name") or "Imported profile").strip()
    internal_name = (data.get("internal_name") or display_name.lower().replace(" ", "_")).strip()
    allowed_models = data.get("allowed_models")
    default_model_id = data.get("default_model_id")
    temperature = data.get("temperature")
    top_p = data.get("top_p")
    max_tokens = data.get("max_tokens")
    short_description = data.get("short_description") or ""
    avatar = data.get("avatar") or ""
    system_prompt = data.get("system_prompt") or ""

    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM ai_profiles WHERE internal_name=?", (internal_name,))
    if _one(cur):
        internal_name = f"{internal_name}_import"
    cur.execute("SELECT COUNT(*) FROM ai_profiles WHERE display_name=?", (display_name,))
    if _one(cur):
        display_name = f"{display_name} (import)"

    profile_id = create_ai_profile(
        conn,
        owner_user_id=owner_user_id,
        internal_name=internal_name,
        display_name=display_name,
        short_description=short_description,
        avatar=avatar,
        system_prompt=system_prompt,
        allowed_models=allowed_models,
        default_model_id=default_model_id,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        is_builtin=is_builtin,
    )
    return profile_id


# ---------- Storage for attachments ----------

def cas_put(db, *, data_dir: Path, sha256: str, mime: str, src_path: str) -> int:
    """
    Ensure the file is present in on-disk CAS (data/cas/<sha256>), de-dupe by sha256, and return the id from the files table.
    """
    cas_root = Path(data_dir) / "cas"
    cas_root.mkdir(parents=True, exist_ok=True)
    cas_path = cas_root / sha256
    mode = read_db_mode(db)

    raw_bytes: Optional[bytes] = None
    if not cas_path.exists():
        raw_bytes = Path(src_path).read_bytes()
        if mode == "strict":
            ct, nonce = encrypt_bytes_for_cas(raw_bytes)
            cas_path.write_bytes(CAS_MAGIC + nonce + ct)
        else:
            cas_path.write_bytes(raw_bytes)

    sha_blob = bytes.fromhex(sha256)
    cur = db.cursor()
    original_name = Path(src_path).name
    cur.execute("SELECT id, original_name FROM files WHERE sha256=?", (sha_blob,))
    row = cur.fetchone()
    if row:
        file_id = int(row[0])
        existing_name = row[1] if len(row) > 1 else None
        if (existing_name is None or str(existing_name).strip() == "") and original_name:
            try:
                cur.execute("UPDATE files SET original_name=? WHERE id=?", (original_name, file_id))
            except Exception:
                log.exception("Failed to backfill original_name for file %s", file_id)
        return file_id

    if raw_bytes is None:
        raw_bytes = Path(src_path).read_bytes()
    size_bytes = len(raw_bytes)
    kind = "image" if mime.startswith("image/") else "other"

    cur.execute(
        "INSERT OR IGNORE INTO files(kind, mime, sha256, size_bytes, width, height, page_count, duration_ms, exif_json, thumb_sha256, original_name, ref_count, created) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (kind, mime, sha_blob, size_bytes, None, None, None, None, None, None, original_name, 0, _now()),
    )
    cur.execute("SELECT id FROM files WHERE sha256=?", (sha_blob,))
    row = cur.fetchone()
    if not row:
        raise RuntimeError("Failed to insert or retrieve file metadata.")
    return int(row[0])


def cas_path_for_file(db, file_id: int, *, data_dir: Path) -> Optional[Path]:
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
    cas_root = Path(data_dir) / "cas"
    path = cas_root / sha_hex
    if not path.exists():
        return None

    mode = read_db_mode(db)
    if mode != "strict":
        return path

    data = path.read_bytes()
    raw_bytes = data
    if data.startswith(CAS_MAGIC) and len(data) >= len(CAS_MAGIC) + 12:
        nonce = data[len(CAS_MAGIC):len(CAS_MAGIC) + 12]
        ct = data[len(CAS_MAGIC) + 12:]
        try:
            raw_bytes = decrypt_bytes_for_cas(ct, nonce)
        except Exception:
            raw_bytes = b""

    tmp_root = Path(data_dir) / "cas_tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_root / sha_hex
    tmp_path.write_bytes(raw_bytes)
    return tmp_path


def list_conversation_files(db, conversation_id: int) -> List[Dict[str, Any]]:
    """
    Return unique files referenced by any message in a conversation, along with usage count.
    """
    cur = db.cursor()
    cur.execute(
        """
        SELECT
            f.id AS file_id,
            f.original_name,
            f.mime,
            f.kind,
            COUNT(*) AS ref_count,
            MIN(m.created) AS first_used,
            f.created
        FROM messages AS m
        JOIN message_files AS mf ON mf.message_id = m.id
        JOIN files AS f ON f.id = mf.file_id
        WHERE m.conversation_id = ? AND mf.role = 'attachment'
        GROUP BY f.id, f.original_name, f.mime, f.kind, f.created
        ORDER BY first_used ASC, f.created ASC, f.id ASC
        """,
        (int(conversation_id),),
    )
    rows = cur.fetchall()
    cols = [c[0] for c in cur.description] if cur.description else []
    if not cols:
        return []
    return [dict(zip(cols, r)) for r in rows]


def list_file_occurrences(db, conversation_id: int, file_id: int) -> List[Dict[str, Any]]:
    """
    Return messages in the conversation that reference the given file as an attachment.
    """
    cur = db.cursor()
    cur.execute(
        """
        SELECT m.id AS message_id, m.sender_type, m.created
        FROM messages AS m
        JOIN message_files AS mf ON mf.message_id = m.id
        WHERE m.conversation_id = ? AND mf.file_id = ? AND mf.role = 'attachment'
        ORDER BY m.created ASC, m.id ASC
        """,
        (int(conversation_id), int(file_id)),
    )
    rows = cur.fetchall()
    cols = [c[0] for c in cur.description] if cur.description else []
    if not cols:
        return []
    return [dict(zip(cols, r)) for r in rows]
