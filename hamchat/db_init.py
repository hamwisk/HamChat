# hamchat/db_init.py
from __future__ import annotations
import logging, os, sys, json, stat
from pathlib import Path
from typing import Optional, Tuple
from .paths import settings_dir
from .settings import load_settings, set_security_mode
from .constants import SCHEMA_VERSION

log = logging.getLogger("db")
DB_FILENAME = "ham_mem.db"

# --- Optional deps (only needed in secure/strict) ---
try:
    from pysqlcipher3 import dbapi2 as sqlcipher  # type: ignore
except Exception:
    sqlcipher = None  # imported lazily when needed

# --- keyring (optional but preferred) ---
try:
    import keyring  # type: ignore
except Exception:
    keyring = None

KEYRING_SERVICE = "HamChat"
KEYRING_DB_ACCOUNT = "sqlcipher-dbkey"
KEYRING_FIELD_ACCOUNT = "field-key-v1"

def ensure_database_ready(data_dir: Path, *, update_settings: bool = True) -> int:
    """Create/verify the database. Return 0 on success, 1 on fatal error.
    If update_settings=True, persist the effective db_mode into settings/app.json."""
    try:
        # Ensure base data dir
        data_dir.mkdir(parents=True, exist_ok=True)

        # Ensure CAS dirs exist early, so any code that assumes they're there
        # (including weird first-run flows) doesn't silently misbehave.
        cas_dir = data_dir / "cas"
        cas_dir.mkdir(parents=True, exist_ok=True)

        cas_tmp_dir = data_dir / "cas_tmp"
        cas_tmp_dir.mkdir(parents=True, exist_ok=True)

        db_path = data_dir / DB_FILENAME
        settings_path = settings_dir().joinpath("app.json")

        if not db_path.exists():
            # First run → choose mode
            mode = _choose_mode()
            log.info("Selected database mode: %s", mode)

            if mode == "open":
                ok = _create_open_db(db_path)
            elif mode in ("secure", "strict"):
                if sqlcipher is None:
                    log.error("SQLCipher not available (pysqlcipher3 not installed).")
                    return 1
                key = _get_or_create_db_key()
                ok = _create_sqlcipher_db(db_path, key, mode)
            else:
                log.error("Unknown db mode: %s", mode)
                return 1

            # Back-fill settings with the chosen mode on creation
            if update_settings:
                cfg = load_settings(settings_path) if settings_path.exists() else {}
                set_security_mode(settings_path, cfg, mode)

            # We should update the settings in app.json now with the `mode` value
            return 0 if ok else 1

        # Existing file → detect mode and verify
        detected, conn = _open_existing(db_path)
        if detected is None:
            log.error("Could not open existing database (neither as SQLite nor SQLCipher).")
            return 1

        log.info("Database found in mode: %s", detected)
        try:
            if not _integrity_check(conn, detected):
                log.error("Integrity check failed.")
                return 1
            # Sanity: must have meta table with mode that matches detected engine
            db_mode = _read_meta_mode(conn)
            if db_mode is None:
                log.error("meta.db_mode missing.")
                return 1
            if db_mode == "open" and detected != "open":
                log.error("Engine mismatch: meta says open, but file decrypts only with SQLCipher.")
                return 1
            if db_mode in ("secure", "strict") and detected == "open":
                log.error("Engine mismatch: meta says %s, but file opens as plaintext.", db_mode)
                return 1
            # Reflect actual db_mode into settings if missing/stale
            if update_settings:
                cfg = load_settings(settings_path) if settings_path.exists() else {}
                set_security_mode(settings_path, cfg, db_mode)
            log.info("Database ready (schema version %s, mode %s).", SCHEMA_VERSION, db_mode)
            return 0
        finally:
            conn.close()

    except Exception:
        log.exception("Unexpected error during DB initialization")
        return 1


# ------------------------- mode selection -------------------------

def _choose_mode() -> str:
    """Return 'open'|'secure'|'strict'. Non-interactive if HAMCHAT_DB_MODE is set."""
    env = (os.getenv("HAMCHAT_DB_MODE") or "").strip().lower()
    if env in {"open", "secure", "strict"}:
        log.info("HAMCHAT_DB_MODE=%s", env)
        return env

    # Simple CLI prompt (first run only); safe default: 'open'
    print(
        "\nHamChat database setup:\n"
        "  1) Open    (fastest; no encryption)\n"
        "  2) Secure  (encrypted database)\n"
        "  3) Strict  (encrypted database + field-level encryption)\n"
    )
    choice = input("Choose [1-3] (default 1): ").strip()
    if choice == "2":
        return "secure"
    if choice == "3":
        return "strict"
    return "open"


# ------------------------- open (SQLite) -------------------------

def _create_open_db(path: Path) -> bool:
    import sqlite3 as sql
    try:
        conn = sql.connect(path)
        _apply_common_pragmas(conn)
        _create_schema(conn, mode="open")
        conn.commit()
        conn.close()
        log.info("New OPEN database created at %s.", path)
        return True
    except Exception:
        log.exception("Failed to create OPEN database.")
        return False


# ------------------------- sqlcipher (encrypted) -------------------------

def _create_sqlcipher_db(path: Path, key: bytes, mode: str) -> bool:
    assert mode in ("secure", "strict")
    try:
        conn = sqlcipher.connect(str(path))  # type: ignore
        cur = conn.cursor()
        _sqlcipher_key(cur, key)

        # Log cipher version for diagnostics
        try:
            cur.execute("PRAGMA cipher_version;")
            row = cur.fetchone()
            log.info("SQLCipher version: %s", (row[0] if row and row[0] else "unknown"))
        except Exception as e:
            log.warning("Unable to read cipher_version: %r", e)

        # If strict, provision field key now (so we're ready for AEAD later)
        if mode == "strict":
            _ = _get_or_create_field_key(existing_only=False)

        _apply_common_pragmas(conn)
        _create_schema(conn, mode=mode)
        conn.commit()

        # Verify connection/encryption with a robust check
        if not _verify_sqlcipher_connection(cur):
            log.error("Post-create verification failed.")
            conn.close()
            return False

        conn.close()
        log.info("New %s SQLCipher database created at %s.", mode.upper(), path)
        return True
    except Exception:
        log.exception("Failed to create SQLCipher database.")
        return False


def _open_existing(path: Path) -> Tuple[Optional[str], "ConnectionLike"]:
    # 1) Try open SQLite
    try:
        import sqlite3 as sql
        conn = sql.connect(path)
        cur = conn.cursor()
        cur.execute("PRAGMA integrity_check;")
        if cur.fetchone()[0] == "ok":
            return "open", conn
        conn.close()
    except Exception:
        pass

    # 2) Try SQLCipher with key from keyring/env
    if sqlcipher is None:
        return None, None  # type: ignore

    key = None
    try:
        key = _get_or_create_db_key(existing_only=True)
    except Exception:
        key = None
    if not key:
        return None, None  # no stored key to try

    try:
        conn = sqlcipher.connect(str(path))  # type: ignore
        cur = conn.cursor()
        _sqlcipher_key(cur, key)

        # Prefer integrity pragma; if it yields nothing, use fallback meta read
        try:
            cur.execute("PRAGMA cipher_integrity_check;")
            row = cur.fetchone()
            if row and isinstance(row[0], str) and row[0].lower() == "ok":
                return "secure_or_strict", conn
        except Exception as e:
            log.debug("cipher_integrity_check unavailable/failed on existing DB: %r", e)

        # Fallback test
        try:
            cur.execute("SELECT value FROM meta WHERE key='schema_version';")
            if cur.fetchone():
                return "secure_or_strict", conn
        except Exception:
            pass

        conn.close()
    except Exception:
        pass

    return None, None  # type: ignore


# ------------------------- helpers -------------------------

def _apply_common_pragmas(conn) -> None:
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON;")
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA synchronous=NORMAL;")
    cur.execute("PRAGMA temp_store=MEMORY;")
    # modest cache; can tune later
    cur.execute("PRAGMA cache_size=-80000;")

def _sqlcipher_key(cur, key: bytes) -> None:
    # Be explicit; defaults vary by build
    cur.execute(f"PRAGMA key = \"x'{key.hex()}'\";")
    cur.execute("PRAGMA cipher_page_size = 4096;")
    cur.execute("PRAGMA kdf_iter = 256000;")
    cur.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA512;")
    cur.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512;")
    # harden memory handling if supported
    try: cur.execute("PRAGMA cipher_memory_security = ON;")
    except Exception: pass

def _integrity_check(conn, detected_mode: str) -> bool:
    cur = conn.cursor()
    if detected_mode == "open":
        cur.execute("PRAGMA integrity_check;")
        return cur.fetchone()[0] == "ok"
    # Encrypted path
    try:
        cur.execute("PRAGMA cipher_integrity_check;")
        row = cur.fetchone()
        if row and isinstance(row[0], str) and row[0].lower() == "ok":
            return True
    except Exception as e:
        log.debug("cipher_integrity_check unavailable during _integrity_check: %r", e)
    # fallback
    try:
        cur.execute("SELECT 1 FROM meta LIMIT 1;")
        return bool(cur.fetchone())
    except Exception:
        return False

def _read_meta_mode(conn) -> Optional[str]:
    cur = conn.cursor()
    try:
        cur.execute("SELECT value FROM meta WHERE key='db_mode';")
        row = cur.fetchone()
        return row[0] if row else None
    except Exception:
        return None

def _verify_sqlcipher_connection(cur) -> bool:
    """
    Robust verification: prefer cipher_integrity_check; if unavailable on this build,
    fall back to a simple meta table read which also exercises decryption.
    """
    # Try integrity pragma
    try:
        cur.execute("PRAGMA cipher_integrity_check;")
        row = cur.fetchone()
        if row and isinstance(row[0], str) and row[0].lower() == "ok":
            return True
    except Exception as e:
        log.debug("cipher_integrity_check unavailable/failed: %r", e)

    # Fallback: can we read from meta?
    try:
        cur.execute("SELECT value FROM meta WHERE key='schema_version';")
        row = cur.fetchone()
        return bool(row and row[0])
    except Exception as e:
        log.debug("Meta read failed during verification: %r", e)
        return False


# ------------------------- key management -------------------------

def _get_or_create_db_key(existing_only: bool = False) -> Optional[bytes]:
    """Get/create the 32-byte SQLCipher DB key. Uses keyring, else env HC_KEY_DB."""
    # 1) keyring
    if keyring is not None:
        val = keyring.get_password(KEYRING_SERVICE, KEYRING_DB_ACCOUNT)
        if val:
            try:
                import base64
                return base64.urlsafe_b64decode(val.encode("ascii"))
            except Exception:
                log.warning("Keyring DB key decode failed; ignoring stored value.")
        if existing_only:
            return None
        # create
        import os, base64
        raw = os.urandom(32)
        keyring.set_password(KEYRING_SERVICE, KEYRING_DB_ACCOUNT,
                             base64.urlsafe_b64encode(raw).decode("ascii"))
        return raw

    # 2) env var fallback
    raw_hex = os.getenv("HC_KEY_DB")
    if raw_hex:
        try:
            return bytes.fromhex(raw_hex)
        except Exception:
            log.error("HC_KEY_DB is not valid hex.")
    if existing_only:
        return None
    # 3) generate ephemeral (discouraged; warn)
    log.warning("No keyring found; generating ephemeral DB key (not persisted). "
                "Set HC_KEY_DB to a 64-hex key to persist.")
    import os
    return os.urandom(32)

def _get_or_create_field_key(existing_only: bool = False) -> Optional[bytes]:
    """Field AEAD key for strict mode (future use)."""
    if keyring is not None:
        k = keyring.get_password(KEYRING_SERVICE, KEYRING_FIELD_ACCOUNT)
        if k:
            try: return bytes.fromhex(k)
            except Exception: log.warning("Field key hex decode failed; ignoring stored value.")
        if existing_only:
            return None
        import os
        raw = os.urandom(32)
        keyring.set_password(KEYRING_SERVICE, KEYRING_FIELD_ACCOUNT, raw.hex())
        return raw

    # env fallback
    raw_hex = os.getenv("HC_KEY_FIELD")
    if raw_hex:
        try: return bytes.fromhex(raw_hex)
        except Exception: log.error("HC_KEY_FIELD is not valid hex.")
    if existing_only:
        return None
    import os
    log.warning("No keyring; generating ephemeral FIELD key.")
    return os.urandom(32)


# ------------------------- schema -------------------------

DDL_CORE = f"""
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Account signup queue (no emails needed)
CREATE TABLE IF NOT EXISTS signup_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  handle TEXT NOT NULL,          -- candidate; must be unique when promoted
  username TEXT NOT NULL,        -- candidate; must be unique when promoted
  email TEXT,
  pw_salt BLOB NOT NULL,         -- scrypt salt (generated at request time)
  pw_hash BLOB NOT NULL,         -- scrypt hash (request-time)
  created INTEGER NOT NULL,
  status TEXT CHECK(status IN ('pending','approved','rejected')) NOT NULL DEFAULT 'pending',
  decided_by INTEGER NULL REFERENCES user_profiles(id) ON DELETE SET NULL,
  decided_at INTEGER NULL,
  note TEXT
);

-- Optional: helper indexes for admin screens
CREATE INDEX IF NOT EXISTS idx_signup_status ON signup_requests(status, created DESC);

-- Core users/auth (you said login is always required)
CREATE TABLE IF NOT EXISTS user_profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT,
  handle TEXT UNIQUE,
  email TEXT UNIQUE,
  created INTEGER,
  updated INTEGER
);

CREATE TABLE IF NOT EXISTS user_auth (
  id INTEGER PRIMARY KEY REFERENCES user_profiles(id) ON DELETE CASCADE,
  username TEXT UNIQUE,
  role TEXT CHECK(role IN ('user','admin')) DEFAULT 'user',
  pw_salt BLOB NOT NULL,
  pw_hash BLOB NOT NULL,
  created INTEGER,
  updated INTEGER,
  last_login INTEGER
);

CREATE TABLE IF NOT EXISTS saved_conversations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER REFERENCES user_profiles(id) ON DELETE CASCADE,
  title TEXT,
  created INTEGER
);

-- Messages: single schema supports plain or AEAD fields
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id INTEGER REFERENCES saved_conversations(id) ON DELETE CASCADE,
  sender_type TEXT CHECK(sender_type IN ('user','assistant','system','tool')),
  sender_id INTEGER,
  content TEXT NULL,              -- used in 'open' and 'secure'
  content_ct BLOB NULL,           -- used in 'strict'
  content_nonce BLOB NULL,        -- used in 'strict'
  content_key_id INTEGER NULL,    -- reserved for future rotation
  metadata TEXT,
  created INTEGER
);

-- Persistent memory skeleton
CREATE TABLE IF NOT EXISTS persistent_memory (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT CHECK(scope IN ('user','conversation','global')),
  user_id INTEGER NULL,
  conversation_id INTEGER NULL,
  subject TEXT,
  content TEXT NULL,
  content_ct BLOB NULL,
  content_nonce BLOB NULL,
  content_key_id INTEGER NULL,
  importance INTEGER DEFAULT 0,
  reinforced_at INTEGER,
  created INTEGER,
  vector_ref TEXT,
  retention_until INTEGER
);

-- File metadata (payloads are stored in encrypted CAS outside DB)
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT CHECK(kind IN ('image','audio','video','doc','other')),
  mime TEXT, sha256 BLOB UNIQUE, size_bytes INTEGER,
  width INTEGER, height INTEGER, page_count INTEGER, duration_ms INTEGER, exif_json TEXT,
  thumb_sha256 BLOB, original_name TEXT,
  ref_count INTEGER DEFAULT 0, created INTEGER
);

-- Admin-only governance
CREATE TABLE IF NOT EXISTS org_policy (
  id INTEGER PRIMARY KEY CHECK(id=1),
  policy_json TEXT NOT NULL,
  version INTEGER NOT NULL,
  updated INTEGER
);

CREATE TABLE IF NOT EXISTS access_grants (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  grantee_user_id INTEGER REFERENCES user_profiles(id) ON DELETE CASCADE,
  resource TEXT, permission TEXT, created INTEGER, expires INTEGER
);

CREATE TABLE IF NOT EXISTS audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER, actor_user_id INTEGER, action TEXT, subject TEXT, details TEXT,
  prev_hash BLOB, hash BLOB
);
"""

# Enforce strict-mode at row level with triggers (optional guard-rails)
DDL_STRICT_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS trg_messages_strict_ins
BEFORE INSERT ON messages
WHEN (SELECT value FROM meta WHERE key='db_mode')='strict'
AND NEW.content IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'strict mode requires encrypted content');
END;

CREATE TRIGGER IF NOT EXISTS trg_messages_strict_upd
BEFORE UPDATE ON messages
WHEN (SELECT value FROM meta WHERE key='db_mode')='strict'
AND NEW.content IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'strict mode requires encrypted content');
END;

CREATE TRIGGER IF NOT EXISTS trg_memory_strict_ins
BEFORE INSERT ON persistent_memory
WHEN (SELECT value FROM meta WHERE key='db_mode')='strict'
AND NEW.content IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'strict mode requires encrypted content');
END;

CREATE TRIGGER IF NOT EXISTS trg_memory_strict_upd
BEFORE UPDATE ON persistent_memory
WHEN (SELECT value FROM meta WHERE key='db_mode')='strict'
AND NEW.content IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'strict mode requires encrypted content');
END;
"""

def _create_schema(conn, mode: str) -> None:
    cur = conn.cursor()
    cur.executescript(DDL_CORE)
    # meta
    cur.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)", (SCHEMA_VERSION,))
    cur.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('db_mode', ?)", (mode,))
    cur.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('created', strftime('%s','now'))")
    cur.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('updated', strftime('%s','now'))")
    # strict guard rails
    if mode == "strict":
        cur.executescript(DDL_STRICT_TRIGGERS)
