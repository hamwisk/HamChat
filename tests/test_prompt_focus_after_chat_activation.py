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


def _panel_window(*, side_open, top_open):
    focus_requests = []
    side_visibility = []
    window = SimpleNamespace(
        _left_open=side_open,
        side_panel=SimpleNamespace(setVisible=lambda visible: side_visibility.append(visible)),
        top_panel=SimpleNamespace(_expanded=top_open),
        _apply_split_sizes=lambda: None,
        _focus_prompt_input=lambda: focus_requests.append(True),
    )
    return window, focus_requests, side_visibility


def test_closing_side_panel_only_focuses_prompt_when_top_panel_is_closed():
    top_open_window, top_open_requests, _ = _panel_window(side_open=True, top_open=True)
    MainWindow.toggle_left_panel(top_open_window)
    assert top_open_requests == []

    top_closed_window, top_closed_requests, visibility = _panel_window(side_open=True, top_open=False)
    MainWindow.toggle_left_panel(top_closed_window)
    assert visibility == [False]
    assert top_closed_requests == [True]


def test_closing_top_panel_focuses_prompt_once():
    focus_requests = []
    sizes = []
    window = SimpleNamespace(
        chat_split=SimpleNamespace(sizes=lambda: [80, 320], setSizes=lambda value: sizes.append(value)),
        _focus_prompt_input=lambda: focus_requests.append(True),
    )

    MainWindow._on_top_closed(window)

    assert sizes == [[0, 400]]
    assert focus_requests == [True]


def test_opening_side_or_top_panel_does_not_request_prompt_focus():
    side_window, side_requests, visibility = _panel_window(side_open=False, top_open=False)
    MainWindow.toggle_left_panel(side_window)
    assert visibility == [True]
    assert side_requests == []

    top_requests = []
    sizes = []
    top_window = SimpleNamespace(
        _top_saved_h=240,
        chat_split=SimpleNamespace(sizes=lambda: [0, 600], setSizes=lambda value: sizes.append(value)),
        _focus_prompt_input=lambda: top_requests.append(True),
    )
    MainWindow._restore_top_panel_height(top_window)
    assert sizes
    assert top_requests == []
