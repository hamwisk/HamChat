from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QMessageBox

from hamchat import db_ops
from hamchat.ui.main_window import MainWindow


class _ChatDisplay:
    def __init__(self, messages=None):
        self.messages = messages or []
        self.cleared = False
        self.appended = []

    def export_messages(self):
        return self.messages

    def clear_messages(self):
        self.cleared = True

    def append_message(self, role, text):
        self.appended.append((role, text))


class _Controller:
    def __init__(self, persisted=False, load_error=None):
        self.persisted = persisted
        self.load_error = load_error
        self.reset = False
        self.loaded = None

    def has_persisted_conversation(self):
        return self.persisted

    def reset_history(self):
        self.reset = True

    def load_conversation(self, conversation_id, messages):
        if self.load_error:
            raise self.load_error
        self.loaded = (conversation_id, messages)


def _window(*, messages=None, persisted=False, load_error=None):
    focus_requests = []
    window = SimpleNamespace(
        chat_display=_ChatDisplay(messages),
        chat_controller=_Controller(persisted, load_error),
        chat_panel=SimpleNamespace(
            on_new_chat_started=lambda: None,
            set_conversation_saved=lambda _id: None,
            set_conversation_title=lambda _title: None,
            set_created_at=lambda _created: None,
        ),
        side_panel=SimpleNamespace(set_active_chat=lambda _id: None, refresh_chats=lambda: None),
        _focus_prompt_input=lambda: focus_requests.append(True),
        _chat_page_offset=0,
        _chat_has_more=False,
        _chat_search_text="",
        _db=object(),
        _on_profile_activated=lambda _id: None,
        _get_conversation_title=lambda _id: "Loaded chat",
    )
    return window, focus_requests


def test_successful_new_chat_requests_prompt_focus():
    window, focus_requests = _window(
        messages=[{"role": "user", "text": "already saved"}], persisted=True,
    )

    assert MainWindow._new_chat(window) is True
    assert window.chat_controller.reset and window.chat_display.cleared
    assert focus_requests == [True]


def test_successful_existing_chat_load_requests_prompt_focus(monkeypatch):
    window, focus_requests = _window()
    monkeypatch.setattr(db_ops, "list_messages", lambda *_args, **_kwargs: [])

    MainWindow._open_conversation(window, 42)

    assert window.chat_controller.loaded == (42, [])
    assert focus_requests == [True]


def test_failed_chat_creation_or_loading_does_not_request_prompt_focus(monkeypatch):
    new_window, new_focus_requests = _window(
        messages=[{"role": "user", "text": "discard me"}], persisted=False,
    )
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.No)

    assert MainWindow._new_chat(new_window) is False
    assert new_focus_requests == []

    load_window, load_focus_requests = _window(load_error=RuntimeError("load failed"))
    monkeypatch.setattr(db_ops, "list_messages", lambda *_args, **_kwargs: [])

    MainWindow._open_conversation(load_window, 42)
    assert load_focus_requests == []


def test_unrelated_chat_refresh_does_not_request_prompt_focus():
    window, focus_requests = _window()

    MainWindow._refresh_user_chats(window)

    assert focus_requests == []
