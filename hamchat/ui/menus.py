# hamchat/ui/menus.py
from __future__ import annotations
from typing import Callable, Iterable
from PyQt6.QtGui import QAction, QActionGroup, QKeySequence
from PyQt6.QtWidgets import QMenuBar, QMenu

class Menus:
    def __init__(
        self,
        *,
        menubar: QMenuBar,
        # spell
        get_spell_enabled: Callable[[], bool],
        get_locale: Callable[[], str],
        get_locales: Callable[[], Iterable[str]],
        toggle_spellcheck: Callable[[bool], None],
        set_spell_locale: Callable[[str], None],
        # theme
        get_variant: Callable[[], str],            # "light" | "dark"
        set_variant: Callable[[str], None],
        # file actions
        new_chat: Callable[[], None],
        app_exit: Callable[[], None],
        toggle_side_panel: Callable[[], None],
        # model (NEW)
        get_current_model: Callable[[], str],
        get_models: Callable[[], Iterable[tuple[str, str]]],
        set_current_model: Callable[[str], None],
        open_model_manager: Callable[[], None],
    ) -> None:
        self.mb = menubar
        self._get_spell_enabled = get_spell_enabled
        self._get_locale = get_locale
        self._get_locales = get_locales
        self._toggle_spell = toggle_spellcheck
        self._set_locale = set_spell_locale
        self._get_variant = get_variant
        self._set_variant = set_variant
        self._new_chat = new_chat
        self._exit = app_exit
        self._toggle_side = toggle_side_panel

        # NEW: model hooks
        self._get_current_model = get_current_model
        self._get_models = get_models
        self._set_current_model = set_current_model
        self._open_model_manager = open_model_manager

    def build(self) -> None:
        self.mb.clear()
        self._file_menu()
        self._edit_menu()
        self._model_menu()
        self._view_menu()

    def _file_menu(self) -> None:
        m = self.mb.addMenu("&File")
        act_new = m.addAction("New Chat", self._new_chat)
        act_new.setShortcut(QKeySequence("Ctrl+N"))
        m.addSeparator()
        act_exit = m.addAction("Exit", self._exit)
        act_exit.setShortcut(QKeySequence("Ctrl+Q"))

    def _edit_menu(self) -> None:
        m = self.mb.addMenu("&Edit")
        # Spellcheck submenu
        sm = m.addMenu("Spellcheck")
        a_toggle = sm.addAction("Enable")
        a_toggle.setCheckable(True)
        a_toggle.setChecked(self._get_spell_enabled())
        a_toggle.toggled.connect(self._toggle_spell)
        sm.addSeparator()

        # Locales grouped by base language
        locales = sorted(self._get_locales() or [])
        by_base: dict[str, list[str]] = {}
        for loc in locales:
            base = loc.split("_", 1)[0]
            by_base.setdefault(base, []).append(loc)

        cur = self._get_locale()
        for base, variants in sorted(by_base.items()):
            sub = QMenu(base, sm)
            group = QActionGroup(sub); group.setExclusive(True)
            for v in sorted(variants):
                act = sub.addAction(v)
                act.setCheckable(True)
                act.setChecked(v == cur and self._get_spell_enabled())
                act.triggered.connect(lambda _=False, vv=v: self._set_locale(vv))
                group.addAction(act)
            sm.addMenu(sub)

    def _model_menu(self) -> None:
        m = self.mb.addMenu("&Model")

        models = list(self._get_models() or [])
        current = self._get_current_model()

        group = QActionGroup(m)
        group.setExclusive(True)

        for model_id, label in models:
            act = m.addAction(label)
            act.setCheckable(True)
            act.setChecked(model_id == current)
            # capture model_id correctly
            act.triggered.connect(
                lambda _checked=False, mid=model_id: self._set_current_model(mid)
            )
            group.addAction(act)

        if models:
            m.addSeparator()

        act_manage = m.addAction("Models…")
        act_manage.triggered.connect(self._open_model_manager)

    def _view_menu(self) -> None:
        m = self.mb.addMenu("&View")
        # Dark mode toggle
        a_dark = m.addAction("Dark mode")
        a_dark.setCheckable(True)
        a_dark.setChecked(self._get_variant() == "dark")
        a_dark.toggled.connect(lambda on: self._set_variant("dark" if on else "light"))
        m.addSeparator()
        m.addAction("Toggle Side Panel", self._toggle_side)
