from __future__ import annotations

import sqlite3
from types import SimpleNamespace
import logging
import requests

from hamchat import db_ops
from hamchat.db_init import _create_schema, _migrate_existing_schema
from hamchat.infra.llm.base import ChatMessage, ModelClient, StreamEvent
from hamchat.infra.llm.backend_adapter import make_stream_func_from_client
from hamchat.infra.llm.ollama_client import OllamaClient, RuntimeContext
from hamchat.ui.chat_controller import ChatController


class Response:
    def __init__(self, lines=()):
        self.lines = list(lines)

    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def raise_for_status(self): return None
    def iter_lines(self, **_kwargs): return iter(self.lines)


def _chat(client, mode):
    return list(client.stream_chat(
        model="thinker", messages=[ChatMessage(role="user", content="hello")], options={},
        prepared_context=RuntimeContext(4096, "runtime"), thinking_mode=mode,
    ))


def test_conversation_thinking_mode_defaults_and_persists():
    conn = sqlite3.connect(":memory:")
    _create_schema(conn, "open")
    conn.execute("INSERT INTO user_profiles(id, name) VALUES(1, 'User')")
    conn.commit()

    first = db_ops.create_conversation(conn, 1, "first")
    second = db_ops.create_conversation(conn, 1, "second", thinking_mode="high")
    db_ops.set_conversation_thinking_mode(conn, first, "low")

    assert db_ops.get_conversation_thinking_mode(conn, first) == "low"
    assert db_ops.get_conversation_thinking_mode(conn, second) == "high"
    # Re-reading each conversation models switching chats/restoring after restart.
    assert db_ops.get_conversation_thinking_mode(conn, first) == "low"


def test_existing_conversations_migrate_to_medium():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta(key, value) VALUES('schema_version', '2026-07-28.1');
        INSERT INTO meta(key, value) VALUES('db_mode', 'open');
        CREATE TABLE saved_conversations (
          id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT, created INTEGER
        );
        INSERT INTO saved_conversations(id, user_id, title, created) VALUES(1, 1, 'old', 0);
    """)

    _migrate_existing_schema(conn, "open")

    assert db_ops.get_conversation_thinking_mode(conn, 1) == "medium"


def test_think_payload_mapping_is_top_level(monkeypatch):
    payloads = []
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.post",
        lambda _url, **kwargs: (payloads.append(kwargs["json"]) or Response(['{"done":true}'])),
    )
    client = OllamaClient()

    for mode, expected in (("off", False), ("low", "low"), ("high", "high")):
        payloads.clear()
        assert _chat(client, mode)[-1].type == "end"
        assert payloads[0]["think"] == expected
        assert "think" not in payloads[0]["options"]

    payloads.clear()
    assert _chat(client, "medium")[-1].type == "end"
    assert "think" not in payloads[0]


def test_explicitly_non_thinking_model_omits_top_level_think(monkeypatch):
    payloads = []
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.post",
        lambda _url, **kwargs: (payloads.append(kwargs["json"]) or Response(['{"done":true}'])),
    )

    assert _chat(OllamaClient(), None)[-1].type == "end"
    assert "think" not in payloads[0]


def test_medium_default_omits_think_from_preload_and_chat(monkeypatch):
    payloads = []

    def post(url, **kwargs):
        payloads.append((url, kwargs["json"]))
        if url.endswith("/api/generate"):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {})
        return Response(['{"done":true}'])

    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.post", post)
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.get",
        lambda *_args, **_kwargs: SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"models": [{"name": "thinker", "context_length": 4096}]},
        ),
    )

    assert list(OllamaClient().stream_chat(
        model="thinker", messages=[ChatMessage(role="user", content="hello")], options={}, thinking_mode="medium",
    ))[-1].type == "end"
    assert ["think" in payload for _url, payload in payloads] == [False, False]


def test_false_rejection_retries_once_with_low_before_output(monkeypatch):
    payloads = []

    def post(_url, **kwargs):
        payloads.append(kwargs["json"])
        lines = ['{"error":"think false is unsupported"}'] if len(payloads) == 1 else ['{"done":true}']
        return Response(lines)

    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.post", post)

    events = _chat(OllamaClient(), "off")

    assert [payload["think"] for payload in payloads] == [False, "low"]
    assert [event.type for event in events] == ["start", "thinking_forced_low", "start", "end"]


def test_thinking_logs_requested_and_effective_modes(monkeypatch, caplog):
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.post",
        lambda _url, **_kwargs: Response(['{"done":true}']),
    )
    caplog.set_level(logging.INFO, logger="llm.ollama")

    list(OllamaClient().stream_chat(
        model="thinker", messages=[ChatMessage(role="user", content="hello")], options={},
        prepared_context=RuntimeContext(4096, "runtime"), thinking_mode="low", requested_thinking_mode="off",
    ))

    assert "requested_thinking_mode=off effective_thinking_mode=low" in caplog.text


def test_false_rejection_never_replays_after_visible_output(monkeypatch):
    payloads = []
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.post",
        lambda _url, **kwargs: (
            payloads.append(kwargs["json"]) or Response([
                '{"message":{"content":"partial"},"done":false}',
                '{"error":"think false is unsupported"}',
            ])
        ),
    )

    events = _chat(OllamaClient(), "off")

    assert len(payloads) == 1
    assert [event.type for event in events] == ["start", "delta", "error"]


def test_unsupported_effort_emits_notice_event_without_retry(monkeypatch):
    payloads = []
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.post",
        lambda _url, **kwargs: (
            payloads.append(kwargs["json"]) or Response(['{"error":"think high is unsupported"}'])
        ),
    )

    events = _chat(OllamaClient(), "high")

    assert len(payloads) == 1
    assert [event.type for event in events] == ["start", "thinking_rejected", "error"]


def test_explicit_unsupported_thinking_retries_once_without_think(monkeypatch):
    payloads = []

    def post(_url, **kwargs):
        payloads.append(kwargs["json"])
        if len(payloads) == 1:
            return Response(['{"error":"gemma3:latest does not support thinking"}'])
        return Response(['{"message":{"content":"answer"},"done":true}'])

    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.post", post)

    events = _chat(OllamaClient(), "high")

    assert len(payloads) == 2
    assert payloads[0]["think"] == "high"
    assert "think" not in payloads[1]
    assert [event.type for event in events] == ["start", "thinking_unsupported", "start", "delta", "end"]


def test_generic_http_error_does_not_probe_without_thinking(monkeypatch):
    payloads = []

    class HttpErrorResponse:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def raise_for_status(self):
            error = requests.HTTPError("400 Client Error")
            error.response = SimpleNamespace(json=lambda: {"error": "invalid request"})
            raise error

    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.post",
        lambda _url, **kwargs: (payloads.append(kwargs["json"]) or HttpErrorResponse()),
    )

    events = _chat(OllamaClient(), "high")

    assert len(payloads) == 1
    assert [event.type for event in events] == ["error"]


def test_timeout_does_not_retry_or_emit_capability_event(monkeypatch):
    payloads = []
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.post",
        lambda _url, **kwargs: (payloads.append(kwargs["json"]) or (_ for _ in ()).throw(requests.Timeout("late"))),
    )

    events = _chat(OllamaClient(), "high")

    assert len(payloads) == 1
    assert [event.type for event in events] == ["error"]


def test_unsupported_retry_keeps_final_messages_callback_once(monkeypatch):
    payloads, snapshots = [], []

    def post(_url, **kwargs):
        payloads.append(kwargs["json"])
        return Response(
            ['{"error":"model does not support thinking"}']
            if len(payloads) == 1 else ['{"done":true}']
        )

    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.post", post)
    list(OllamaClient().stream_chat(
        model="thinker", messages=[ChatMessage(role="user", content="hello")], options={},
        prepared_context=RuntimeContext(4096, "runtime"), thinking_mode="high",
        final_messages_callback=lambda messages: snapshots.append(messages),
    ))

    assert len(snapshots) == 1
    assert len(payloads) == 2


def test_non_ollama_adapter_does_not_receive_thinking_argument():
    class OtherClient(ModelClient):
        def stream_chat(self, *, model, messages, options):
            assert options == {"temperature": 0.1}
            yield StreamEvent(type="delta", text="answer")
            yield StreamEvent(type="end")

    stream = make_stream_func_from_client(
        OtherClient(), model="remote", build_messages=lambda prompt: [ChatMessage("user", prompt)],
        build_options=lambda: {"temperature": 0.1},
    )
    assert list(stream("hello", stop_fn=lambda: False)) == ["answer"]


class Panel:
    def __init__(self):
        self.mode = None
        self.notices = []
        self.unverified = None

    def set_thinking_mode(self, mode, *, enabled=None): self.mode = (mode, enabled)
    def set_thinking_support_unverified(self, unverified): self.unverified = unverified
    def show_thinking_notice(self, text): self.notices.append(text)


class Session:
    def get_model_capabilities(self, _model): return {}
    def get_model_metadata(self, _model): return {"family": "gptoss"}


def test_gpt_oss_off_normalizes_to_low_and_notifies_once():
    panel = Panel()
    state = SimpleNamespace(
        _model_client=SimpleNamespace(prepare_runtime_context=lambda: None),
        _model_name="custom-tag", _session=Session(), _thinking_mode="off",
        _thinking_panel=panel, _active_ticket=-1, _conv_id=None, _thinking_notice_shown=False,
    )
    state._thinking_model_info = ChatController._thinking_model_info.__get__(state)
    state._refresh_thinking_controls = ChatController._refresh_thinking_controls.__get__(state)
    state._set_thinking_mode = ChatController._set_thinking_mode.__get__(state)

    assert ChatController._request_thinking_modes(state) == ("off", "low")
    assert state._thinking_mode == "low"
    assert panel.mode == ("low", True)
    assert panel.notices == ["Thinking can’t be disabled for this model, so HamChat has set it to Low."]
    assert ChatController._request_thinking_modes(state) == ("low", "low")
    assert len(panel.notices) == 1


class UnknownSession:
    def get_model_capabilities(self, _model): return {}
    def get_model_metadata(self, _model): return {"family": "gemma4"}


def test_unknown_gemma_family_stays_enabled_and_marks_support_unverified():
    panel = Panel()
    state = SimpleNamespace(
        _model_client=SimpleNamespace(prepare_runtime_context=lambda: None),
        _model_name="my-gemma-custom-tag", _session=UnknownSession(), _thinking_mode="high",
        _thinking_panel=panel, _active_ticket=-1,
    )
    state._thinking_model_info = ChatController._thinking_model_info.__get__(state)

    ChatController._refresh_thinking_controls(state)

    assert panel.mode == ("high", True)
    assert panel.unverified is True
    assert ChatController._request_thinking_modes(state) == ("high", "high")


def test_explicit_non_thinking_capability_disables_controls_and_omits_mode():
    class NonThinkingSession:
        def get_model_capabilities(self, _model): return {"thinking": False}
        def get_model_metadata(self, _model): return {"family": "gemma4"}

    panel = Panel()
    state = SimpleNamespace(
        _model_client=SimpleNamespace(prepare_runtime_context=lambda: None),
        _model_name="non-thinking", _session=NonThinkingSession(), _thinking_mode="high",
        _thinking_panel=panel, _active_ticket=-1,
    )
    state._thinking_model_info = ChatController._thinking_model_info.__get__(state)

    ChatController._refresh_thinking_controls(state)

    assert panel.mode == ("high", False)
    assert panel.unverified is False
    assert panel.notices == [
        "non-thinking does not support Ollama thinking controls. "
        "HamChat will omit the thinking setting for this model."
    ]
    assert ChatController._request_thinking_modes(state) == (None, None)


def test_controller_learns_true_only_for_clean_explicit_unknown_generation():
    learned = []
    state = SimpleNamespace(
        _active_ticket=5, _active_row=None, _assistant_buf=[], chat=SimpleNamespace(set_streaming=lambda _value: None),
        _thinking_ticket=-1, _thinking_generation_state={
            5: {"model": "gemma4:12b", "unknown_at_submission": True,
                "explicit_think_sent": True, "unsupported": False},
        },
        _set_learned_thinking_capability=lambda model, value: learned.append((model, value)),
        _refresh_thinking_controls=lambda: None, _refresh_ham_mem_control=lambda: None,
    )

    ChatController._on_job_finished(state, 5, "ok")

    assert learned == [("gemma4:12b", True)]


def test_controller_unsupported_notice_keeps_false_after_success():
    panel = Panel()
    learned = []
    state = SimpleNamespace(
        _active_ticket=7, _active_row=None, _assistant_buf=[], chat=SimpleNamespace(set_streaming=lambda _value: None),
        _thinking_ticket=-1, _thinking_panel=panel, _model_name="gemma3:latest",
        _thinking_generation_state={
            7: {"model": "gemma3:latest", "unknown_at_submission": True,
                "explicit_think_sent": True, "unsupported": False},
        },
        _set_learned_thinking_capability=lambda model, value: learned.append((model, value)),
        _refresh_thinking_controls=lambda: None, _refresh_ham_mem_control=lambda: None,
    )

    ChatController._on_job_notice(state, 7, "thinking_unsupported", "high")
    ChatController._on_job_finished(state, 7, "ok")

    assert learned == [("gemma3:latest", False)]
    # The real refresh path owns the persistent capability notice.
    assert panel.notices == []
