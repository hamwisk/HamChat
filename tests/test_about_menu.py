from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication, QMenuBar, QMessageBox

from hamchat.constants import APP_NAME, SCHEMA_VERSION, __version__
from hamchat.ui.main_window import MainWindow
from hamchat.ui.menus import Menus
from hamchat.update_controller import UpdateController
from hamchat.updates import DecisionReason, RemoteCheckStatus, UpdateDecision


def _menus(menubar, show_about):
    return Menus(
        menubar=menubar,
        get_spell_enabled=lambda: False,
        get_locale=lambda: "en_GB",
        get_locales=lambda: [],
        toggle_spellcheck=lambda _enabled: None,
        set_spell_locale=lambda _locale: None,
        get_variant=lambda: "dark",
        set_variant=lambda _variant: None,
        new_chat=lambda: None,
        import_chat=lambda: None,
        app_exit=lambda: None,
        toggle_side_panel=lambda: None,
        get_current_model=lambda: "chat",
        get_models=lambda: [],
        set_current_model=lambda _model: None,
        open_model_manager=lambda: None,
        show_about=show_about,
    )


def test_help_menu_about_action_is_created_and_routed():
    app = QApplication.instance() or QApplication([])
    called = []
    bar = QMenuBar()
    menus = _menus(bar, lambda: called.append(True))
    menus.build()

    help_menu = next(action.menu() for action in bar.actions() if action.text() == "&Help")
    about_action = next(action for action in help_menu.actions() if action.text() == "About HamChat")
    about_action.trigger()

    assert called == [True]
    bar.deleteLater()
    app.processEvents()


def test_about_dialog_uses_application_constants(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        QMessageBox,
        "about",
        lambda parent, title, text: captured.update(parent=parent, title=title, text=text),
    )
    window = object()

    MainWindow._show_about(window)

    assert captured["parent"] is window
    assert captured["title"] == f"About {APP_NAME}"
    assert APP_NAME in captured["text"]
    assert __version__ in captured["text"]
    assert SCHEMA_VERSION in captured["text"]


def test_manual_no_update_message_includes_installed_version():
    messages = []
    state = SimpleNamespace(
        _stopping=False,
        _handle_candidate=lambda *_args: None,
        _message=lambda _icon, _title, message: messages.append(message),
        _finish_check=lambda *_args: None,
    )
    result = SimpleNamespace(
        status=RemoteCheckStatus.NO_ELIGIBLE_UPDATE,
        decision=UpdateDecision(DecisionReason.REMOTE_NOT_NEWER),
        manifest=None,
    )

    UpdateController._checked(state, SimpleNamespace(result=lambda: result), startup=False, manual=True)

    assert messages == [f"HamChat {__version__} is already up to date."]
