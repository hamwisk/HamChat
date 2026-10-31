from __future__ import annotations

import logging

from hamchat.core.session import SessionManager
from hamchat.core.settings import Settings
from hamchat.infra.llm.base import ChatMessage
from hamchat.infra.llm.ollama_client import OllamaClient


class Response:
    def __init__(self, payload=None, lines=()):
        self.payload = payload or {}
        self.lines = list(lines)

    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def raise_for_status(self): return None
    def json(self): return self.payload
    def iter_lines(self, **_kwargs): return iter(self.lines)


def test_per_model_allocation_persists_and_restores(tmp_path):
    settings = Settings(tmp_path / "app.json")
    session = SessionManager(settings, "solo", None)

    assert session.get_model_context_allocation("one") == "auto"
    assert session.get_model_context_num_ctx("one") is None
    session.set_model_context_allocation("one", "high")
    session.set_model_context_allocation("two", "low")

    restored = SessionManager(Settings(tmp_path / "app.json"), "solo", None)
    assert restored.get_model_context_allocation("one") == "high"
    assert restored.get_model_context_num_ctx("one") == 16384
    assert restored.get_model_context_allocation("two") == "low"
    assert restored.get_model_context_num_ctx("two") == 4096


def test_auto_omits_num_ctx_and_tiers_reach_preload_and_chat(monkeypatch):
    requests_seen = []

    def post(url, **kwargs):
        requests_seen.append((url, kwargs["json"]))
        if url.endswith("/api/generate"):
            return Response({})
        return Response(lines=['{"done":true}'])

    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.post", post)
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.get",
        lambda *_a, **_k: Response({"models": [{"name": "model", "context_length": 4096}]}),
    )

    for requested in (None, 4096, 8192, 16384):
        requests_seen.clear()
        options = {"num_predict": 512}
        if requested is not None:
            options["num_ctx"] = requested
        events = list(OllamaClient().stream_chat(
            model="model", messages=[ChatMessage(role="user", content="hello")], options=options,
        ))
        assert events[-1].type == "end"
        preload, chat = requests_seen
        assert preload[0].endswith("/api/generate") and chat[0].endswith("/api/chat")
        if requested is None:
            assert "num_ctx" not in preload[1]["options"]
            assert "num_ctx" not in chat[1]["options"]
        else:
            assert preload[1]["options"]["num_ctx"] == requested
            assert chat[1]["options"]["num_ctx"] == requested


def test_discovered_context_not_requested_tier_controls_planning(monkeypatch, caplog):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs["json"]))
        if url.endswith("/api/generate"):
            return Response({})
        return Response(lines=['{"done":true}'])

    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.post", post)
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.get",
        lambda *_a, **_k: Response({"models": [{"name": "model", "context_length": 4096}]}),
    )
    caplog.set_level(logging.INFO, logger="llm.ollama")

    events = list(OllamaClient().stream_chat(
        model="model",
        messages=[ChatMessage(role="user", content="x" * 6000)],
        options={"num_ctx": 16384, "num_predict": 512},
    ))

    assert events[-1].type == "end"
    assert calls[-1][1]["options"]["num_ctx"] == 16384
    assert "requested_num_ctx=16384 effective_context_length=4096" in caplog.text


def test_allocation_cache_reuse_and_invalidation(monkeypatch):
    calls = []

    def post(url, **_kwargs):
        calls.append("preload" if url.endswith("generate") else "chat")
        return Response({})

    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.post", post)
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.get",
        lambda *_a, **_k: (calls.append("ps") or Response({"models": [{"name": "model", "context_length": 8192}]})),
    )
    client = OllamaClient()

    client.prepare_runtime_context(model="model", options={"num_ctx": 8192})
    client.prepare_runtime_context(model="model", options={"num_ctx": 8192})
    assert calls == ["preload", "ps"]
    client.invalidate_runtime_context(model="model")
    client.prepare_runtime_context(model="model", options={"num_ctx": 8192})
    assert calls == ["preload", "ps", "preload", "ps"]


def test_fallback_with_allocation_remains_retryable(monkeypatch):
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.post",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("offline")),
    )
    client = OllamaClient()

    first = client.prepare_runtime_context(model="model", options={"num_ctx": 16384})
    second = client.prepare_runtime_context(model="model", options={"num_ctx": 16384})

    assert (first.context_length, first.source) == (4096, "fallback")
    assert (second.context_length, second.source) == (4096, "fallback")


def test_generation_limit_persistence_validation_and_explicit_precedence(tmp_path):
    import pytest
    from types import SimpleNamespace
    from hamchat.ui.chat_controller import ChatController

    settings = Settings(tmp_path / "app.json")
    session = SessionManager(settings, "solo", None)
    assert session.get_model_generation_limit("one") == 4096
    session.set_model_generation_limit("one", 7000)
    restored = SessionManager(Settings(tmp_path / "app.json"), "solo", None)
    assert restored.get_model_generation_limit("one") == 7000
    assert restored.get_model_generation_limit("two") == 4096
    for invalid in (0, -1, True, "4096", 2.5):
        with pytest.raises(ValueError):
            session.set_model_generation_limit("one", invalid)
    state = SimpleNamespace(_session=restored, _model_client=OllamaClient(), _model_name="one")
    options = {}
    ChatController._apply_model_context_allocation(state, options)
    assert options["num_predict"] == 7000
    options["num_predict"] = 128
    ChatController._apply_model_context_allocation(state, options)
    assert options["num_predict"] == 128
