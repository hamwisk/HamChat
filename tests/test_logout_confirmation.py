from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from hamchat.ui.main_window import MainWindow


class _Dialog:
    class ButtonRole:
        AcceptRole = "accept"
        RejectRole = "reject"

    outcome = None
    last = None

    def __init__(self, parent):
        self.parent = parent
        self.buttons = {}
        self.default_button = None
        self.escape_button = None
        self._clicked = None
        type(self).last = self

    def setWindowTitle(self, title):
        self.title = title

    def setText(self, text):
        self.text = text

    def addButton(self, label, role):
        button = object()
        self.buttons[label] = (button, role)
        return button

    def setDefaultButton(self, button):
        self.default_button = button

    def setEscapeButton(self, button):
        self.escape_button = button

    def exec(self):
        if self.outcome in self.buttons:
            self._clicked = self.buttons[self.outcome][0]

    def clickedButton(self):
        return self._clicked


class _Session:
    def __init__(self):
        self.logout_calls = 0

    def logout(self):
        self.logout_calls += 1


def _window():
    session = _Session()
    window = SimpleNamespace(
        session=session,
        chat_controller=SimpleNamespace(
            clear_memory_vectors=lambda: None,
            clear_transient_thinking=lambda: None,
            reset_chat_memory_preferences=lambda: None,
        ),
        top_panel=SimpleNamespace(close_panel=lambda: None),
        _new_chat=lambda: None,
    )
    return window, session


@pytest.mark.parametrize("outcome", ["Cancel", None])
def test_cancel_or_dismissed_logout_leaves_session_untouched(monkeypatch, outcome):
    window, session = _window()
    _Dialog.outcome = outcome
    monkeypatch.setattr("hamchat.ui.main_window.QMessageBox", _Dialog)

    MainWindow._do_logout(window)

    assert session.logout_calls == 0
    assert _Dialog.last.default_button is _Dialog.last.buttons["Cancel"][0]
    assert _Dialog.last.escape_button is _Dialog.last.buttons["Cancel"][0]


def test_confirmed_logout_calls_session_logout_once(monkeypatch):
    window, session = _window()
    _Dialog.outcome = "Log out"
    monkeypatch.setattr("hamchat.ui.main_window.QMessageBox", _Dialog)

    MainWindow._do_logout(window)

    assert session.logout_calls == 1
    assert _Dialog.last.parent is window
    assert _Dialog.last.title == "Confirm logout"
    assert _Dialog.last.text == "Are you sure you want to log out?"
    assert set(_Dialog.last.buttons) == {"Log out", "Cancel"}


class _Signal:
    def __init__(self):
        self.connected = []

    def connect(self, slot):
        self.connected.append(slot)


def test_side_panel_logout_signal_still_reaches_confirmed_handler():
    signal_names = (
        "sig_open_form", "ai_profiles_manager", "create_conversation",
        "open_user_settings", "open_memory_view", "open_theme_manager",
        "open_conversation", "rename_conversation", "delete_conversation",
        "import_conversation", "load_older_chats", "chat_search_changed",
        "profile_activated", "request_login", "request_logout",
    )
    side_panel = SimpleNamespace(**{name: _Signal() for name in signal_names})
    top_panel = SimpleNamespace(sig_closed=_Signal(), sig_opened=_Signal())
    chat_display = SimpleNamespace(bubbleAction=_Signal())
    chat_panel = SimpleNamespace(
        attachmentOpenRequested=_Signal(), attachmentAttachRequested=_Signal(),
        attachmentScrollRequested=_Signal(),
    )
    handler = lambda: None
    window = SimpleNamespace(
        side_panel=side_panel, top_panel=top_panel, chat_display=chat_display,
        chat_panel=chat_panel, _do_logout=handler,
    )
    for name in (
        "_open_test_form", "_open_ai_profiles_manager", "_on_top_closed",
        "_restore_top_panel_height", "_new_chat", "_open_memory_manager",
        "_open_conversation", "_rename_conversation", "_delete_conversation",
        "_import_chat", "_load_older_chats", "_search_chats",
        "_on_profile_activated", "_open_login_flow", "_on_bubble_action",
        "_on_attachment_open_requested", "_on_attachment_attach_requested",
        "_on_attachment_scroll_requested",
    ):
        setattr(window, name, lambda *args: None)

    MainWindow._wire_signals(window)

    assert side_panel.request_logout.connected == [handler]
