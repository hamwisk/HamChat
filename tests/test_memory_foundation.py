from __future__ import annotations

import sqlite3

import pytest

from hamchat import db_init, db_ops


def make_db(mode="open"):
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    db_init._create_schema(conn, mode)
    return conn


def add_user(conn, name, role="user"):
    return db_ops.create_user(conn, name=name, handle=name, email=None, username=name,
                              password="test-password", role=role)


def test_memory_migration_when_table_is_absent():
    conn = make_db(); conn.execute("DROP TABLE persistent_memory")
    conn.execute("UPDATE meta SET value='2025-12-06.1' WHERE key='schema_version'"); conn.commit()
    db_init._migrate_existing_schema(conn, "open")
    assert db_ops.read_schema_version(conn) == "2026-07-28.1"
    assert "owner_user_id" in {r[1] for r in conn.execute("PRAGMA table_info(persistent_memory)")}


def test_current_schema_startup_is_a_noop_and_failed_migration_preserves_version(monkeypatch):
    conn = make_db()
    before = conn.execute("SELECT sql FROM sqlite_master WHERE name='persistent_memory'").fetchone()[0]
    db_init._migrate_existing_schema(conn, "open")
    assert conn.execute("SELECT sql FROM sqlite_master WHERE name='persistent_memory'").fetchone()[0] == before
    conn.execute("UPDATE meta SET value='2025-12-06.1' WHERE key='schema_version'"); conn.commit()
    monkeypatch.setattr(db_init, "_migrate_memory_table", lambda cur, mode: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError): db_init._migrate_existing_schema(conn, "open")
    assert db_ops.read_schema_version(conn) == "2025-12-06.1"


def test_provisional_memory_migration_copies_only_unambiguous_records():
    conn = make_db(); conn.execute("DROP TABLE persistent_memory")
    conn.execute("CREATE TABLE persistent_memory (id INTEGER PRIMARY KEY, scope TEXT, user_id INTEGER, conversation_id INTEGER, subject TEXT, content TEXT, content_ct BLOB, content_nonce BLOB, content_key_id INTEGER, importance INTEGER, reinforced_at INTEGER, created INTEGER, vector_ref TEXT, retention_until INTEGER)")
    user = add_user(conn, "user")
    conn.execute("INSERT INTO persistent_memory(scope,user_id,content,importance,created) VALUES('user',?,?,?,?)", (user, "kept", 1, 1))
    conn.execute("INSERT INTO persistent_memory(scope,user_id,content) VALUES('global',NULL,'discarded')")
    conn.execute("UPDATE meta SET value='2025-12-06.1' WHERE key='schema_version'"); conn.commit()
    db_init._migrate_existing_schema(conn, "open")
    assert [m["content"] for m in db_ops.list_memories(conn, owner_user_id=user)] == ["kept"]


def test_memory_crud_and_owner_scope_target_rules():
    conn = make_db(); user, other, admin = add_user(conn, "user"), add_user(conn, "other"), add_user(conn, "admin", "admin")
    chat = db_ops.create_conversation(conn, user, "chat")
    profile = db_ops.create_ai_profile(conn, owner_user_id=user, internal_name="p", display_name="P")
    memory = db_ops.create_memory(conn, owner_user_id=user, content="x", scope="chat", conversation_id=chat, weight=.25)
    db_ops.update_memory(conn, owner_user_id=user, memory_id=memory, content="y", scope="profile", profile_id=profile, weight=.75)
    assert db_ops.get_memory(conn, owner_user_id=user, memory_id=memory)["weight"] == .75
    assert db_ops.get_memory(conn, owner_user_id=other, memory_id=memory) is None
    with pytest.raises(PermissionError): db_ops.create_memory(conn, owner_user_id=user, content="x", scope="global")
    with pytest.raises(PermissionError): db_ops.create_memory(conn, owner_user_id=admin, content="x", scope="chat", conversation_id=chat)
    with pytest.raises(ValueError): db_ops.create_memory(conn, owner_user_id=user, content="x", scope="user", weight=2)
    global_id = db_ops.create_memory(conn, owner_user_id=admin, content="g", scope="global")
    assert db_ops.get_memory(conn, owner_user_id=admin, memory_id=global_id)["scope"] == "global"


def test_strict_memory_never_writes_plaintext(monkeypatch):
    conn = make_db("strict")
    monkeypatch.setattr(db_ops._dbi, "_get_or_create_field_key", lambda existing_only=False: b"k" * 32)
    user = add_user(conn, "user")
    memory = db_ops.create_memory(conn, owner_user_id=user, content="secret", scope="user")
    content, ciphertext = conn.execute("SELECT content, content_ct FROM persistent_memory WHERE id=?", (memory,)).fetchone()
    assert content is None and ciphertext is not None
    assert db_ops.get_memory(conn, owner_user_id=user, memory_id=memory)["content"] == "secret"


def test_secure_mode_uses_database_encryption_policy_not_field_encryption():
    conn = make_db("secure")
    user = add_user(conn, "user")
    memory = db_ops.create_memory(conn, owner_user_id=user, content="protected by SQLCipher", scope="user")
    assert conn.execute("SELECT content FROM persistent_memory WHERE id=?", (memory,)).fetchone()[0] == "protected by SQLCipher"
