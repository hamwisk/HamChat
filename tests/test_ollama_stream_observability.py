from __future__ import annotations

import logging

import pytest
import requests

from hamchat.infra.llm.backend_adapter import make_stream_func_from_client
from hamchat.infra.llm.base import ChatMessage, ModelClient, StreamEvent
from hamchat.infra.llm.ollama_client import FALLBACK_CONTEXT_LENGTH, OllamaClient
from hamchat.infra.llm.thread_broker import Job, _Worker


class FakeResponse:
    def __init__(self, lines=(), json_data=None, error=None):
        self.lines = list(lines)
        self.json_data = {} if json_data is None else json_data
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        if self.error:
            raise self.error

    def iter_lines(self, decode_unicode=True):
        assert decode_unicode is True
        return iter(self.lines)

    def json(self):
        return self.json_data


def install_transport(monkeypatch, *, ps_models=(), chat_lines=('{"done":true}',), preload_error=None):
    calls = []

    def post(url, **kwargs):
        if url.endswith("/api/generate"):
            calls.append("preload")
            assert kwargs["json"]["prompt"] == ""
            assert kwargs["json"]["stream"] is False
            if preload_error:
                raise preload_error
            return FakeResponse(json_data={"done": True})
        assert url.endswith("/api/chat")
        calls.append("chat")
        return FakeResponse(lines=chat_lines)

    def get(url, **_kwargs):
        assert url.endswith("/api/ps")
        calls.append("ps")
        return FakeResponse(json_data={"models": list(ps_models)})

    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.post", post)
    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.get", get)
    return calls


def stream(client, model="test:latest", options=None):
    return list(client.stream_chat(
        model=model,
        messages=[ChatMessage(role="user", content="hello")],
        options=options if options is not None else {"temperature": 0.2, "num_ctx": 4096},
    ))


def test_preload_ps_then_real_chat_order_and_runtime_logging(monkeypatch, caplog):
    calls = install_transport(monkeypatch, ps_models=[{"name": "test:latest", "context_length": 8192}], chat_lines=[
        '{"message":{"content":"Hello"},"done":false}',
        '{"model":"test:latest","done":true,"done_reason":"stop",'
        '"prompt_eval_count":11,"eval_count":2,"total_duration":123}',
    ])
    caplog.set_level(logging.INFO, logger="llm.ollama")

    events = stream(OllamaClient())

    assert calls == ["preload", "ps", "chat"]
    assert [(event.type, event.text) for event in events] == [
        ("start", ""), ("delta", "Hello"), ("end", ""),
    ]
    assert "effective_context_length=8192 effective_context_source=runtime" in caplog.text
    assert "done_reason='stop'" in caplog.text
    assert "prompt_eval_count=11" in caplog.text
    assert "visible_chunk_count=1 visible_char_count=5" in caplog.text


def test_preparation_uses_long_read_timeout_and_logs_elapsed_time(monkeypatch, caplog):
    seen_timeouts = []

    def post(url, **kwargs):
        if url.endswith("/api/generate"):
            seen_timeouts.append(kwargs["timeout"])
            return FakeResponse(json_data={})
        return FakeResponse(lines=['{"done":true}'])

    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.post", post)
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.get",
        lambda *_args, **_kwargs: FakeResponse(json_data={"models": [{"name": "test:latest", "context_length": 8192}]}),
    )
    caplog.set_level(logging.INFO, logger="llm.ollama")

    assert OllamaClient().prepare_runtime_context(model="test:latest", options={}).source == "runtime"

    assert seen_timeouts == [(5, 300)]
    assert "connect_timeout=5s read_timeout=300s elapsed_seconds=" in caplog.text


def test_preparation_connection_failure_uses_short_connect_timeout(monkeypatch):
    seen_timeouts = []

    def connection_failure(_url, **kwargs):
        seen_timeouts.append(kwargs["timeout"])
        raise OSError("connection refused")

    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.post", connection_failure)

    resolved = OllamaClient().prepare_runtime_context(model="test:latest", options={})

    assert (resolved.context_length, resolved.source) == (FALLBACK_CONTEXT_LENGTH, "fallback")
    assert seen_timeouts == [(5, 300)]


def test_preparation_read_timeout_returns_retryable_fallback(monkeypatch, caplog):
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.exceptions.ReadTimeout("cold model")),
    )
    caplog.set_level(logging.WARNING, logger="llm.ollama")
    client = OllamaClient()

    resolved = client.prepare_runtime_context(model="test:latest", options={})

    assert (resolved.context_length, resolved.source) == (FALLBACK_CONTEXT_LENGTH, "fallback")
    assert client.get_runtime_context(model="test:latest", options={}).source == "fallback"
    assert "read_timeout=300s" in caplog.text


def test_chat_stream_timeout_remains_client_timeout(monkeypatch):
    timeouts = []

    def post(url, **kwargs):
        timeouts.append((url, kwargs["timeout"]))
        if url.endswith("/api/generate"):
            return FakeResponse(json_data={})
        return FakeResponse(lines=['{"done":true}'])

    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.post", post)
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.get",
        lambda *_args, **_kwargs: FakeResponse(json_data={"models": [{"name": "test:latest", "context_length": 8192}]}),
    )
    client = OllamaClient(timeout=123)

    assert stream(client)[-1].type == "end"
    assert timeouts == [
        ("http://127.0.0.1:11434/api/generate", (5, 300)),
        ("http://127.0.0.1:11434/api/chat", 123),
    ]


def test_adapter_prepares_context_before_building_messages(monkeypatch):
    calls = install_transport(monkeypatch, ps_models=[{"name": "test:latest", "context_length": 8192}])
    client = OllamaClient()

    def build_messages(_prompt):
        assert client.get_runtime_context(model="test:latest", options={}).source == "cache"
        return [ChatMessage(role="user", content="hello")]

    stream_func = make_stream_func_from_client(
        client, model="test:latest", build_messages=build_messages, build_options=lambda: {},
    )

    assert list(stream_func("hello", stop_fn=lambda: False)) == []
    assert calls == ["preload", "ps", "chat"]


def test_exact_active_model_match_ignores_other_loaded_models(monkeypatch):
    calls = install_transport(monkeypatch, ps_models=[
        {"name": "other:latest", "context_length": 32768},
        {"model": "wanted:latest", "context_length": 8192},
    ])

    resolved = OllamaClient().prepare_runtime_context(model="wanted:latest", options={})

    assert resolved.context_length == 8192
    assert resolved.source == "runtime"
    assert calls == ["preload", "ps"]


def test_cached_context_reuses_without_preload_or_ps(monkeypatch):
    calls = install_transport(monkeypatch, ps_models=[{"name": "test:latest", "context_length": 8192}])
    client = OllamaClient()

    first = client.prepare_runtime_context(model="test:latest", options={"num_ctx": 4096})
    second = client.prepare_runtime_context(model="test:latest", options={"num_ctx": 4096})

    assert (first.context_length, first.source) == (8192, "runtime")
    assert (second.context_length, second.source) == (8192, "cache")
    assert calls == ["preload", "ps"]


def test_model_and_client_change_prepare_again(monkeypatch):
    calls = install_transport(monkeypatch, ps_models=[
        {"name": "one", "context_length": 4096}, {"name": "two", "context_length": 8192},
    ])
    client = OllamaClient()
    client.prepare_runtime_context(model="one", options={})
    client.prepare_runtime_context(model="two", options={})
    OllamaClient().prepare_runtime_context(model="one", options={})

    assert calls == ["preload", "ps", "preload", "ps", "preload", "ps"]


def test_context_option_change_uses_a_separate_cache_entry(monkeypatch):
    calls = install_transport(monkeypatch, ps_models=[{"name": "test:latest", "context_length": 8192}])
    client = OllamaClient()
    client.prepare_runtime_context(model="test:latest", options={"num_ctx": 4096})
    client.prepare_runtime_context(model="test:latest", options={"num_ctx": 8192})

    assert calls == ["preload", "ps", "preload", "ps"]


def test_transport_failure_invalidates_confirmed_context(monkeypatch):
    calls = []
    chat_failure = OSError("connection reset")

    def post(url, **_kwargs):
        if url.endswith("/api/generate"):
            calls.append("preload")
            return FakeResponse(json_data={})
        calls.append("chat")
        if calls.count("chat") == 1:
            raise chat_failure
        return FakeResponse(lines=['{"done":true}'])

    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.post", post)
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.get",
        lambda *_args, **_kwargs: (calls.append("ps") or FakeResponse(json_data={"models": [{"name": "test:latest", "context_length": 8192}]})),
    )
    client = OllamaClient()
    client.prepare_runtime_context(model="test:latest", options={})

    failed_events = stream(client, options={})
    assert failed_events[-1].type == "error"
    assert client.get_runtime_context(model="test:latest", options={}).source == "fallback"
    client.prepare_runtime_context(model="test:latest", options={})
    assert calls == ["preload", "ps", "chat", "preload", "ps"]


@pytest.mark.parametrize("context_length", [None, "8192", 0, -1, True])
def test_invalid_context_length_uses_retryable_fallback(monkeypatch, context_length):
    calls = install_transport(monkeypatch, ps_models=[{"name": "test:latest", "context_length": context_length}])
    client = OllamaClient()

    first = client.prepare_runtime_context(model="test:latest", options={})
    second = client.prepare_runtime_context(model="test:latest", options={})

    assert (first.context_length, first.source) == (FALLBACK_CONTEXT_LENGTH, "fallback")
    assert (second.context_length, second.source) == (FALLBACK_CONTEXT_LENGTH, "fallback")
    assert calls == ["preload", "ps", "preload", "ps"]


def test_missing_active_model_uses_fallback(monkeypatch):
    install_transport(monkeypatch, ps_models=[{"name": "other", "context_length": 8192}])

    resolved = OllamaClient().prepare_runtime_context(model="test:latest", options={})

    assert (resolved.context_length, resolved.source) == (FALLBACK_CONTEXT_LENGTH, "fallback")


def test_preload_failure_keeps_chat_possible_and_later_retryable(monkeypatch):
    calls = install_transport(monkeypatch, preload_error=OSError("offline"))
    client = OllamaClient()

    events = stream(client, options={})

    assert events[-1].type == "end"
    assert calls == ["preload", "chat"]
    assert client.get_runtime_context(model="test:latest", options={}).source == "fallback"
    calls[:] = []
    install_transport(monkeypatch, ps_models=[{"name": "test:latest", "context_length": 8192}])
    assert client.prepare_runtime_context(model="test:latest", options={}).source == "runtime"


def test_ps_failure_keeps_chat_possible_and_later_retryable(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.post",
        lambda url, **_kwargs: (calls.append("preload" if url.endswith("generate") else "chat") or FakeResponse(lines=['{"done":true}'])),
    )
    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.get", lambda *_a, **_k: (_ for _ in ()).throw(OSError("ps down")))
    client = OllamaClient()

    assert stream(client, options={})[-1].type == "end"
    assert calls == ["preload", "chat"]
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.get",
        lambda *_a, **_k: FakeResponse(json_data={"models": [{"name": "test:latest", "context_length": 8192}]}),
    )
    assert client.prepare_runtime_context(model="test:latest", options={}).source == "runtime"


def test_hosts_models_and_context_configurations_do_not_share_cache(monkeypatch):
    calls = install_transport(monkeypatch, ps_models=[
        {"name": "one", "context_length": 4096}, {"name": "two", "context_length": 8192},
    ])
    first_host = OllamaClient(base_url="http://first")
    second_host = OllamaClient(base_url="http://second")
    first_host.prepare_runtime_context(model="one", options={"num_ctx": 4096})
    first_host.prepare_runtime_context(model="two", options={"num_ctx": 4096})
    first_host.prepare_runtime_context(model="one", options={"num_ctx": 8192})
    second_host.prepare_runtime_context(model="one", options={"num_ctx": 4096})

    assert calls == ["preload", "ps"] * 4


def test_request_logging_labels_runtime_cache_and_fallback(monkeypatch, caplog):
    calls = install_transport(monkeypatch, ps_models=[{"name": "test:latest", "context_length": 8192}])
    caplog.set_level(logging.INFO, logger="llm.ollama")
    client = OllamaClient()
    stream(client)
    stream(client)
    assert calls == ["preload", "ps", "chat", "chat"]
    assert "effective_context_source=runtime" in caplog.text
    assert "effective_context_source=cache" in caplog.text

    install_transport(monkeypatch, preload_error=OSError("offline"))
    stream(OllamaClient(), options={})
    assert "effective_context_length=4096 effective_context_source=fallback" in caplog.text


def test_thinking_and_visible_content_are_counted_separately(monkeypatch, caplog):
    install_transport(monkeypatch, ps_models=[{"name": "test:latest", "context_length": 8192}], chat_lines=[
        '{"message":{"thinking":"consider","content":"answer"},"done":false}',
        '{"done":true,"done_reason":"stop"}',
    ])
    caplog.set_level(logging.INFO, logger="llm.ollama")

    events = stream(OllamaClient())

    assert [event.text for event in events if event.type == "delta"] == ["answer"]
    assert "thinking_chunk_count=1 thinking_char_count=8" in caplog.text
    assert "visible_chunk_count=1 visible_char_count=6" in caplog.text


def test_ollama_error_object_remains_a_stream_error(monkeypatch):
    install_transport(
        monkeypatch,
        ps_models=[{"name": "test:latest", "context_length": 8192}],
        chat_lines=['{"error":"model overloaded"}'],
    )

    events = stream(OllamaClient())

    assert events[-1].type == "error"
    assert "model overloaded" in events[-1].error


def test_malformed_ndjson_and_eof_without_terminal_remain_errors(monkeypatch):
    install_transport(monkeypatch, ps_models=[{"name": "test:latest", "context_length": 8192}], chat_lines=["not-json", '{"done":true}'])
    assert "protocol error" in stream(OllamaClient())[-1].error

    install_transport(monkeypatch, ps_models=[{"name": "test:latest", "context_length": 8192}], chat_lines=['{"message":{"content":"partial"},"done":false}'])
    events = stream(OllamaClient())
    assert [event.text for event in events if event.type == "delta"] == ["partial"]
    assert "interrupted stream" in events[-1].error


class ErrorClient(ModelClient):
    def stream_chat(self, *, model, messages, options):
        yield StreamEvent(type="delta", text="partial")
        yield StreamEvent(type="error", error="backend failed")


def test_adapter_error_propagates_to_broker_as_error_status():
    stream_func = make_stream_func_from_client(
        ErrorClient(), model="fake", build_messages=lambda _prompt: [], build_options=lambda: {},
    )
    worker = _Worker(Job(ticket=7, func=stream_func, args=("prompt",)))
    tokens = []
    errors = []
    finished = []
    worker.token.connect(lambda ticket, text: tokens.append((ticket, text)))
    worker.error.connect(lambda ticket, message: errors.append((ticket, message)))
    worker.finished.connect(lambda ticket, status: finished.append((ticket, status)))
    worker.run()

    assert tokens == [(7, "partial")]
    assert errors and "StreamError: backend failed" in errors[0][1]
    assert finished == [(7, "error")]
