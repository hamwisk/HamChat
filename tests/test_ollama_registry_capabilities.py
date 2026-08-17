from __future__ import annotations

import json
from pathlib import Path

from hamchat.infra.llm.ollama_registry import (
    _load_registry,
    refresh_registry,
    update_model_capability,
)


def _write_registry(path: Path, *, digest: str = "old") -> None:
    path.write_text(json.dumps({
        "schema": 1,
        "source": "old-source",
        "last_refresh": "before",
        "models": [{
            "name": "gemma3:latest", "digest": digest, "first_seen": 1,
            "last_seen": 2, "available": True, "context": 8192,
            "family": "gemma3", "ctx_source": "override", "custom": "kept",
            "capabilities": {"vision": True},
        }],
        "unrelated": {"keep": True},
    }), encoding="utf-8")


def test_schema_one_loads_without_eager_rewrite_and_update_preserves_fields(tmp_path):
    path = tmp_path / "models.json"
    _write_registry(path)

    loaded = _load_registry(path)
    assert loaded["schema"] == 1
    assert "thinking" not in loaded["models"][0]["capabilities"]
    assert update_model_capability("gemma3:latest", "thinking", False, registry_path=path)

    saved = json.loads(path.read_text(encoding="utf-8"))
    entry = saved["models"][0]
    assert saved["schema"] == 2
    assert saved["unrelated"] == {"keep": True}
    assert entry["capabilities"] == {"vision": True, "thinking": False}
    assert entry["context"] == 8192
    assert entry["custom"] == "kept"


def test_capability_update_does_not_rewrite_when_value_is_already_stored(tmp_path):
    path = tmp_path / "models.json"
    _write_registry(path)
    assert update_model_capability("gemma3:latest", "thinking", True, registry_path=path)
    first_contents = path.read_text(encoding="utf-8")

    assert not update_model_capability("gemma3:latest", "thinking", True, registry_path=path)
    assert path.read_text(encoding="utf-8") == first_contents


def test_changed_digest_resets_learned_thinking_but_preserves_other_fields(tmp_path, monkeypatch):
    path = tmp_path / "models.json"
    _write_registry(path)
    update_model_capability("gemma3:latest", "thinking", True, registry_path=path)

    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_registry.requests.get",
        lambda *_args, **_kwargs: type("Response", (), {"json": lambda self: {
            "models": [{"name": "gemma3:latest", "digest": "new"}],
        }})(),
    )
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_registry._probe_model",
        lambda *_args, **_kwargs: {"vision": False, "context": 16384, "family": "gemma3"},
    )

    refreshed = refresh_registry(registry_path=path)
    entry = refreshed["models"][0]
    assert refreshed["schema"] == 2
    assert entry["digest"] == "new"
    assert entry["capabilities"] == {"vision": False}
    assert entry["custom"] == "kept"
    assert entry["first_seen"] == 1


def test_show_capabilities_are_normalized_and_persisted(tmp_path, monkeypatch):
    path = tmp_path / "models.json"
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_registry.requests.get",
        lambda *_args, **_kwargs: type("Response", (), {"json": lambda self: {
            "models": [{"name": "chat:latest", "digest": "one"}],
        }})(),
    )
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_registry.requests.post",
        lambda *_args, **_kwargs: type("Response", (), {"json": lambda self: {
            "details": {"family": "chat"},
            "capabilities": [" Completion ", "vision", "completion", 7, ""],
        }})(),
    )

    refreshed = refresh_registry(registry_path=path)

    assert refreshed["models"][0]["ollama_capabilities"] == ["completion", "vision"]
    assert refreshed["models"][0]["capabilities"]["vision"] is False
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["models"][0]["ollama_capabilities"] == ["completion", "vision"]


def test_unchanged_legacy_entry_is_enriched_once_then_uses_cached_path(tmp_path, monkeypatch):
    path = tmp_path / "models.json"
    _write_registry(path, digest="same")
    probe_calls = []
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_registry.requests.get",
        lambda *_args, **_kwargs: type("Response", (), {"json": lambda self: {
            "models": [{"name": "gemma3:latest", "digest": "same"}],
        }})(),
    )
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_registry._probe_model",
        lambda *_args, **_kwargs: (probe_calls.append(True) or {"ollama_capabilities": ["completion"]}),
    )

    first = refresh_registry(registry_path=path)
    second = refresh_registry(registry_path=path)

    assert first["models"][0]["ollama_capabilities"] == ["completion"]
    assert second["models"][0]["ollama_capabilities"] == ["completion"]
    assert probe_calls == [True]


def test_failed_legacy_enrichment_stays_unknown_and_is_retryable(tmp_path, monkeypatch):
    path = tmp_path / "models.json"
    _write_registry(path, digest="same")
    probe_calls = []
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_registry.requests.get",
        lambda *_args, **_kwargs: type("Response", (), {"json": lambda self: {
            "models": [{"name": "gemma3:latest", "digest": "same"}],
        }})(),
    )
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_registry._probe_model",
        lambda *_args, **_kwargs: (probe_calls.append(True) or {}),
    )

    first = refresh_registry(registry_path=path)
    second = refresh_registry(registry_path=path)

    assert "ollama_capabilities" not in first["models"][0]
    assert "ollama_capabilities" not in second["models"][0]
    assert probe_calls == [True, True]


def test_embedding_model_is_retained_in_registry_for_non_chat_consumers(tmp_path, monkeypatch):
    path = tmp_path / "models.json"
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_registry.requests.get",
        lambda *_args, **_kwargs: type("Response", (), {"json": lambda self: {
            "models": [{"name": "nomic-embed-text", "digest": "embed"}],
        }})(),
    )
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_registry._probe_model",
        lambda *_args, **_kwargs: {"ollama_capabilities": ["embedding"]},
    )

    refreshed = refresh_registry(registry_path=path)

    assert [entry["name"] for entry in refreshed["models"]] == ["nomic-embed-text"]
    assert refreshed["models"][0]["ollama_capabilities"] == ["embedding"]
