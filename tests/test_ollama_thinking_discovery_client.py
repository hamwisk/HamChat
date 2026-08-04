from __future__ import annotations

import requests
from types import SimpleNamespace

from hamchat.infra.llm.base import ChatMessage
from hamchat.infra.llm.ollama_client import OllamaClient, RuntimeContext


class Response:
    def __init__(self, lines):
        self.lines = lines

    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def raise_for_status(self): return None
    def iter_lines(self, **_kwargs): return iter(self.lines)


def _request(client, mode="high", callback=None):
    return list(client.stream_chat(
        model="gemma3:latest", messages=[ChatMessage("user", "hello")], options={},
        prepared_context=RuntimeContext(4096, "runtime"), thinking_mode=mode,
        final_messages_callback=callback,
    ))


def test_unsupported_thinking_retries_once_and_preserves_callback(monkeypatch):
    payloads, snapshots = [], []

    def post(_url, **kwargs):
        payloads.append(kwargs["json"])
        if len(payloads) == 1:
            return Response(['{"error":"gemma3:latest does not support thinking"}'])
        return Response(['{"message":{"content":"answer"},"done":true}'])

    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.post", post)
    events = _request(OllamaClient(), callback=lambda messages: snapshots.append(messages))

    assert [payload.get("think") for payload in payloads] == ["high", None]
    assert [event.type for event in events] == ["start", "thinking_unsupported", "start", "delta", "end"]
    assert len(snapshots) == 1


def test_falsey_requests_http_400_json_error_still_retries_without_think(monkeypatch):
    payloads = []
    bad_response = requests.Response()
    bad_response.status_code = 400
    bad_response.url = "http://127.0.0.1:11434/api/chat"
    bad_response._content = b'{"error":"\\\"gemma3:latest\\\" does not support thinking"}'
    assert not bad_response  # requests.Response is falsey for HTTP 400.

    def post(_url, **kwargs):
        payloads.append(kwargs["json"])
        return bad_response if len(payloads) == 1 else Response(['{"done":true}'])

    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.post", post)
    events = _request(OllamaClient())

    assert [payload.get("think") for payload in payloads] == ["high", None]
    assert [event.type for event in events] == ["thinking_unsupported", "start", "end"]
    assert not any(event.type == "error" for event in events)


def test_generic_http_error_does_not_retry_or_emit_discovery(monkeypatch):
    payloads = []

    class ErrorResponse(Response):
        def raise_for_status(self):
            error = requests.HTTPError("400 Client Error")
            error.response = SimpleNamespace(json=lambda: {"error": "invalid request"})
            raise error

    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.post",
        lambda _url, **kwargs: (payloads.append(kwargs["json"]) or ErrorResponse([])),
    )
    events = _request(OllamaClient())

    assert len(payloads) == 1
    assert [event.type for event in events] == ["error"]


def test_gpt_oss_false_rejection_still_retries_with_low(monkeypatch):
    payloads = []

    def post(_url, **kwargs):
        payloads.append(kwargs["json"])
        return Response(['{"error":"think false is unsupported"}'] if len(payloads) == 1 else ['{"done":true}'])

    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.post", post)
    events = _request(OllamaClient(), mode="off")

    assert [payload.get("think") for payload in payloads] == [False, "low"]
    assert [event.type for event in events] == ["start", "thinking_forced_low", "start", "end"]
