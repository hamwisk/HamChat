from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from hamchat.infra.llm.ollama_client import OllamaClient
from hamchat.ui.main_window import MainWindow


def _window(client, events):
    controller = SimpleNamespace(
        _model_client=client,
        _model_name="chat",
        clear_memory_vectors=lambda: events.append("vectors"),
        hard_kill=lambda: events.append("hard_kill"),
    )
    return SimpleNamespace(
        _close_cleanup_started=False,
        chat_controller=controller,
        session=SimpleNamespace(get_model_id=lambda: "chat", get_model_metadata=lambda _name: {"ollama_capabilities": ["completion"]}),
    )


def test_close_cancels_before_unloading_current_ollama_model_once(monkeypatch):
    events = []
    client = OllamaClient()
    monkeypatch.setattr(client, "unload_model_if_running", lambda *args, **kwargs: events.append(("unload", args, kwargs)))
    window = _window(client, events)

    MainWindow._cleanup_for_close(window)
    MainWindow._cleanup_for_close(window)

    assert events == ["vectors", "hard_kill", ("unload", ("chat",), {"capabilities": ["completion"]})]


def test_close_skips_non_ollama_and_unload_errors(monkeypatch):
    events = []
    non_ollama_window = _window(object(), events)
    MainWindow._cleanup_for_close(non_ollama_window)
    assert events == ["vectors", "hard_kill"]

    client = OllamaClient()
    monkeypatch.setattr(client, "unload_model_if_running", lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()))
    error_window = _window(client, events)
    MainWindow._cleanup_for_close(error_window)
    assert error_window._close_cleanup_started is True
