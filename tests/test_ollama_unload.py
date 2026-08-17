from __future__ import annotations

from hamchat.infra.llm.ollama_client import OllamaClient


class _Response:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {}
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return self.payload


def _ps(names):
    return _Response({"models": [{"name": name} for name in names]})


def test_unload_all_reports_when_ps_is_empty(monkeypatch):
    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.get", lambda *_args, **_kwargs: _ps([]))

    result = OllamaClient().unload_all_models()

    assert result.loaded_count == 0
    assert result.remaining == ()


def test_completion_model_uses_generate_unload_payload_and_verifies(monkeypatch):
    ps = iter([_ps(["chat"]), _ps([])])
    posts = []
    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.get", lambda *_args, **_kwargs: next(ps))
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.post",
        lambda url, **kwargs: (posts.append((url, kwargs["json"])) or _Response()),
    )

    result = OllamaClient().unload_all_models(capabilities_for=lambda _name: ["completion"])

    assert result.unloaded == ("chat",)
    assert result.remaining == ()
    assert posts == [("http://127.0.0.1:11434/api/generate", {
        "model": "chat", "prompt": "", "stream": False, "keep_alive": 0,
    })]


def test_embedding_model_uses_embed_unload_payload(monkeypatch):
    ps = iter([_ps(["nomic-embed-text"]), _ps([])])
    posts = []
    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.get", lambda *_args, **_kwargs: next(ps))
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.post",
        lambda url, **kwargs: (posts.append((url, kwargs["json"])) or _Response()),
    )

    OllamaClient().unload_all_models(capabilities_for=lambda _name: ["embedding"])

    assert posts == [("http://127.0.0.1:11434/api/embed", {
        "model": "nomic-embed-text", "input": "", "keep_alive": 0,
    })]


def test_unknown_registry_capabilities_are_resolved_with_show(monkeypatch):
    ps = iter([_ps(["chat"]), _ps([])])
    posts = []
    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.get", lambda *_args, **_kwargs: next(ps))

    def post(url, **kwargs):
        posts.append((url, kwargs["json"]))
        if url.endswith("/api/show"):
            return _Response({"capabilities": ["completion"]})
        return _Response()

    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.post", post)

    result = OllamaClient().unload_all_models(capabilities_for=lambda _name: None)

    assert result.unloaded == ("chat",)
    assert [url for url, _payload in posts] == [
        "http://127.0.0.1:11434/api/show", "http://127.0.0.1:11434/api/generate",
    ]


def test_unknown_capabilities_are_skipped_without_guessing(monkeypatch):
    ps = iter([_ps(["unknown"]), _ps(["unknown"])])
    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.get", lambda *_args, **_kwargs: next(ps))
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.post",
        lambda *_args, **_kwargs: _Response({}),
    )

    result = OllamaClient().unload_all_models(capabilities_for=lambda _name: None)

    assert result.skipped == ("unknown",)
    assert result.remaining == ("unknown",)


def test_per_model_failure_does_not_abort_remaining_unloads(monkeypatch):
    ps = iter([_ps(["broken", "embedding"]), _ps(["broken"])])
    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.get", lambda *_args, **_kwargs: next(ps))

    def post(url, **kwargs):
        if kwargs["json"]["model"] == "broken":
            return _Response(error=RuntimeError("unavailable"))
        return _Response()

    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.post", post)

    result = OllamaClient().unload_all_models(
        capabilities_for=lambda name: ["completion"] if name == "broken" else ["embedding"],
    )

    assert result.failed == ("broken",)
    assert result.unloaded == ("embedding",)
    assert result.remaining == ("broken",)


def test_post_unload_ps_marks_a_runner_that_remains_loaded_as_failed(monkeypatch):
    ps = iter([_ps(["chat"]), _ps(["chat"])])
    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.get", lambda *_args, **_kwargs: next(ps))
    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.post", lambda *_args, **_kwargs: _Response())

    result = OllamaClient().unload_all_models(capabilities_for=lambda _name: ["completion"])

    assert result.unloaded == ()
    assert result.failed == ("chat",)
    assert result.remaining == ("chat",)


def test_shutdown_unloads_only_an_exact_running_completion_model(monkeypatch):
    posts = []
    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.get", lambda *_args, **_kwargs: _ps(["chat", "other", "nomic-embed-text"]))
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.post",
        lambda url, **kwargs: (posts.append((url, kwargs["json"])) or _Response()),
    )

    unloaded = OllamaClient().unload_model_if_running("chat", capabilities=["completion"])

    assert unloaded is True
    assert posts == [("http://127.0.0.1:11434/api/generate", {
        "model": "chat", "prompt": "", "stream": False, "keep_alive": 0,
    })]


def test_shutdown_does_not_load_or_unload_absent_or_embedding_models(monkeypatch):
    posts = []
    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.get", lambda *_args, **_kwargs: _ps(["nomic-embed-text"]))
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.post",
        lambda url, **kwargs: (posts.append((url, kwargs["json"])) or _Response()),
    )
    client = OllamaClient()

    assert client.unload_model_if_running("chat", capabilities=["completion"]) is False
    assert client.unload_model_if_running("nomic-embed-text", capabilities=["embedding"]) is False
    assert posts == []


def test_shutdown_timeout_or_unload_failure_is_non_fatal(monkeypatch):
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("offline")),
    )
    assert OllamaClient().unload_model_if_running("chat", capabilities=["completion"]) is False

    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.get", lambda *_args, **_kwargs: _ps(["chat"]))
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.post",
        lambda *_args, **_kwargs: _Response(error=ConnectionError("offline")),
    )
    assert OllamaClient().unload_model_if_running("chat", capabilities=["completion"]) is False
