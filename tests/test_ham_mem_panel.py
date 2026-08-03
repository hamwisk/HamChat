from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from hamchat import db_ops
from hamchat.db_init import _create_schema, _migrate_existing_schema
from hamchat.infra.llm.backend_adapter import make_stream_func_from_client
from hamchat.infra.llm.base import ChatMessage, ModelClient, StreamEvent
from hamchat.infra.llm.thread_broker import StreamChunk
from hamchat.ui.chat_controller import ChatController


def test_conversation_ham_mem_defaults_and_persists_separately():
    conn = sqlite3.connect(":memory:")
    _create_schema(conn, "open")
    conn.execute("INSERT INTO user_profiles(id, name) VALUES(1, 'u')")
    first = db_ops.create_conversation(conn, 1, "first")
    second = db_ops.create_conversation(conn, 1, "second", use_ham_mem=False)
    db_ops.set_conversation_use_ham_mem(conn, first, False)

    assert db_ops.get_conversation_use_ham_mem(conn, first) is False
    assert db_ops.get_conversation_use_ham_mem(conn, second) is False
    assert db_ops.get_conversation_use_ham_mem(conn, db_ops.create_conversation(conn, 1, "new")) is True


def test_legacy_conversation_migrates_ham_mem_to_enabled():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES('schema_version', '2026-08-03.1');
        INSERT INTO meta VALUES('db_mode', 'open');
        CREATE TABLE saved_conversations (
          id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT, created INTEGER,
          thinking_mode TEXT NOT NULL DEFAULT 'medium'
        );
        INSERT INTO saved_conversations VALUES(1, 1, 'legacy', 0, 'medium');
    """)

    _migrate_existing_schema(conn, "open")

    assert db_ops.get_conversation_use_ham_mem(conn, 1) is True


def test_final_memory_snapshot_comes_only_from_final_messages():
    class Client(ModelClient):
        def stream_chat(self, *, model, messages, options):
            assert [message.content for message in messages] == ["included", "question"]
            yield StreamEvent(type="end")

    stream = make_stream_func_from_client(
        Client(), model="remote",
        build_messages=lambda _prompt: [
            ChatMessage("system", "included", metadata={"ham_mem_view": "included"}),
            ChatMessage("user", "question"),
        ],
        on_final_messages=ChatController._final_memory_snapshot,
    )

    updates = list(stream("question", stop_fn=lambda: False))
    assert updates == [StreamChunk(type="memory_snapshot", text="included")]


def test_disabled_ham_mem_snapshot_bypasses_retrieval():
    calls = []
    service = SimpleNamespace(snapshot_context=lambda **_kwargs: calls.append(True))
    state = SimpleNamespace(_memory_service=service, _session=SimpleNamespace(current=SimpleNamespace(user_id=1)))

    assert ChatController._memory_snapshot(state, False) == ([], [])
    assert calls == []


class Panel:
    def __init__(self): self.memory = ""
    def set_memory_snapshot(self, value): self.memory = value


def test_memory_snapshot_uses_ticket_and_never_enters_assistant_buffer():
    panel = Panel()
    state = SimpleNamespace(_active_ticket=4, _thinking_panel=panel, _assistant_buf=["answer"])

    ChatController._on_job_memory_snapshot(state, 3, "old")
    ChatController._on_job_memory_snapshot(state, 4, "included memory")

    assert panel.memory == "included memory"
    assert state._assistant_buf == ["answer"]
