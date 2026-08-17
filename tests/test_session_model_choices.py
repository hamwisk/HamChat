from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from hamchat.core.session import SessionManager


class _Settings:
    def get(self, _key, default=None):
        return default


def _session(monkeypatch, models):
    session = SessionManager(_Settings(), "solo", None)
    monkeypatch.setattr(session, "_load_all_models", lambda: models)
    return session


def test_chat_choices_filter_only_explicitly_non_completion_ollama_models(monkeypatch):
    models = [
        {"name": "chat", "available": True, "ollama_capabilities": ["completion"]},
        {"name": "nomic-embed-text", "available": True, "ollama_capabilities": ["embedding"]},
        {"name": "legacy", "available": True},
    ]

    choices = _session(monkeypatch, models).get_model_choices()

    assert choices == [("chat", "chat"), ("legacy", "legacy")]
    assert models[1]["name"] == "nomic-embed-text"


def test_non_ollama_models_are_not_filtered_by_ollama_capabilities(monkeypatch):
    models = [
        {"name": "remote-embedding", "backend": "openai", "available": True, "ollama_capabilities": []},
    ]

    assert _session(monkeypatch, models).get_model_choices() == [("remote-embedding", "remote-embedding")]
