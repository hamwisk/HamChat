# hamchat/ui/main_window.py
from __future__ import annotations
import sys, logging, json
from typing import Optional
from pathlib import Path
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QStatusBar, QSplitter, QVBoxLayout, QHBoxLayout,
    QFrame, QLabel, QMessageBox
)

from hamchat.paths import settings_dir
from hamchat.ui.theme import ensure_theme, select_variant, apply_theme, export_qml_tokens
from hamchat.ui.menus import Menus
from hamchat.core.settings import Settings
from hamchat.core.session import SessionManager
from hamchat.core.spell_highlighter import get_available_locales, DictionaryFactory
from .chat_controller import ChatController
from .widgets.side_panel import SidePanel
from .widgets.chat_panel import ChatPanel
from .widgets.top_panel import TopPanel
from .widgets.chat_display import ChatDisplay
from .widgets.test_form import TestForm

log = logging.getLogger("ui")

EDGE_WIDTH = 10


class EdgeToggleBar(QFrame):
    def __init__(self, side: str, on_click, parent=None):
        super().__init__(parent)
        self.side = side
        self.on_click = on_click
        self.setFixedWidth(EDGE_WIDTH)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"Toggle {side} panel")
        self.setStyleSheet("background:#30343b;")
    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and callable(self.on_click):
            self.on_click()


def _hbox(parent):
    w = QFrame(parent)
    lay = QHBoxLayout(w); lay.setContentsMargins(0,0,0,0); lay.setSpacing(0)
    w._lay = lay
    return w

def _vbox(parent):
    w = QFrame(parent)
    lay = QVBoxLayout(w); lay.setContentsMargins(0,0,0,0); lay.setSpacing(0)
    w._lay = lay
    return w

def _settings_path():
    return settings_dir() / "app.json"

def _load_app_cfg() -> dict:
    try:
        return json.loads(_settings_path().read_text("utf-8"))
    except Exception:
        return {}

def _save_app_cfg(cfg: dict):
    p = _settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


class MainWindow(QMainWindow):
    def __init__(
            self, runtime_mode: str,
            server_url: Optional[str] = None,
            parent: Optional[QWidget] = None,
            db_conn: Optional[str] = None,
            db_mode: Optional[str] = None
    ):
        super().__init__(parent)
        self.runtime_mode = runtime_mode
        self.server_url = server_url
        self._db_mode = db_mode
        self._db = db_conn  # ← hold sqlite/sqlcipher connection
        self._models_available = None

        self._build_ui()
        self._wire_signals()

        self._theme = None
        self._variant = "dark"  # default; may be overridden by saved cfg
        self._init_theme()

        # --- Load settings once up front
        app_cfg = Settings(settings_dir() / "app.json")
        self.session = SessionManager(app_cfg, self.runtime_mode, self.server_url)
        self.session.prefsChanged.connect(self._apply_prefs)

        self.side_panel.bind_session(self.session)

        # Side panel: provide loaders for user-specific lists
        self.side_panel.set_loaders(
            list_chats=self._load_user_chats,
            # list_profiles can stay as default for now
        )

        # initial apply
        self._apply_prefs(self.session.current.prefs)        # Restore theme variant before first apply

        # Menus now use session getters/setters
        self.menus = Menus(
            menubar=self.menuBar(),
            get_spell_enabled=lambda: self.session.current.prefs.spellcheck_enabled,
            get_locale=lambda: self.session.current.prefs.locale,
            get_locales=lambda: get_available_locales(),
            toggle_spellcheck=self.session.set_spell_enabled,
            set_spell_locale=self.session.set_locale,
            get_variant=lambda: self.session.current.prefs.theme_variant,
            set_variant=self.session.set_theme_variant,
            new_chat=self._new_chat,
            app_exit=self.close,
            toggle_side_panel=self.toggle_left_panel,
            # NEW: model menu wiring
            get_current_model=self.session.get_model_id,
            get_models=self.session.get_model_choices,
            set_current_model=self._on_model_changed_from_menu,
            open_model_manager=self._open_model_manager,
        )
        self.menus.build()

        from hamchat.infra.llm.ollama_client import OllamaClient
        from hamchat.ui.chat_controller import ChatController
        model_id = self.session.get_model_id()
        ollama = OllamaClient()  # uses http://127.0.0.1:11434 by default
        self.chat_controller = ChatController(
            self.chat_display,
            model_client=ollama,
            model_name=model_id,
            parent=self,
            db=self._db,
            session=self.session,
        )

        try:    # When the controller lazily creates a new saved conversation, refresh the side panel list
            self.chat_controller.conversation_started.connect(self._on_conversation_started)
        except Exception:
            pass
        try:    # When a conversation is forked, open it just like selecting from side panel
            self.chat_controller.forked_conversation.connect(self._open_conversation)
        except Exception:
            pass
        try:    # Warn if non-vision model and user tried to send attachments
            self.chat_display.sig_send_payload.connect(self._on_send_payload_from_ui)
        except Exception:
            pass

        self._refresh_status()

    def _build_ui(self):
        self.setWindowTitle(f"HamChat — {self.runtime_mode.upper()}")
        self.resize(1200, 800)

        # Central widget with a single horizontal splitter:
        # [ left_container ] | [ chat_area ]
        cw = QWidget(self)
        self.setCentralWidget(cw)
        root = QVBoxLayout(cw); root.setContentsMargins(0,0,0,0); root.setSpacing(0)

        self.outer_split = QSplitter(Qt.Orientation.Horizontal, cw)
        root.addWidget(self.outer_split, 1)

        # --- LEFT CONTAINER: edge bar + side panel
        left_container = _hbox(self.outer_split)
        self.left_edge = EdgeToggleBar("left", self.toggle_left_panel, left_container)
        self.side_panel = SidePanel(left_container)
        self.side_panel.setMinimumWidth(220)
        left_container._lay.addWidget(self.left_edge)
        left_container._lay.addWidget(self.side_panel)

        # --- CHAT AREA (center column): TopPanel + inner splitter(ChatDisplay | right_container)
        chat_area = _vbox(self.outer_split)

        # Top panel lives *inside* chat_area now (won't affect left panel)
        self.top_panel = TopPanel(parent=chat_area)
        chat_area._lay.addWidget(self.top_panel)

        # Inner splitter: [ ChatDisplay ] | [ right_container ]
        self.inner_split = QSplitter(Qt.Orientation.Horizontal, chat_area)
        chat_area._lay.addWidget(self.inner_split, 1)

        # Center chat view
        self.chat_display = ChatDisplay(self.inner_split)
        self.inner_split.addWidget(self.chat_display)

        # Right container: chat_panel + edge bar
        right_container = _hbox(self.inner_split)
        self.chat_panel = ChatPanel(right_container, chat_display=self.chat_display)
        self.chat_panel.setMinimumWidth(260)
        self.right_edge = EdgeToggleBar("right", self.toggle_right_panel, right_container)
        right_container._lay.addWidget(self.chat_panel)
        right_container._lay.addWidget(self.right_edge)
        self.inner_split.addWidget(right_container)

        # Splitter policies / stretch
        # Left rail should not steal stretch; center owns it; right is optional
        self.outer_split.addWidget(left_container)
        self.outer_split.addWidget(chat_area)
        self.outer_split.setCollapsible(0, False)
        self.outer_split.setCollapsible(1, False)
        self.outer_split.setStretchFactor(0, 0)
        self.outer_split.setStretchFactor(1, 1)

        self.inner_split.setCollapsible(0, False)  # chat view
        self.inner_split.setCollapsible(1, False)  # right panel container
        self.inner_split.setStretchFactor(0, 1)
        self.inner_split.setStretchFactor(1, 0)

        # saved widths (panel-only; edge bar is separate)
        self._left_saved_w = 240  # default side panel width
        self._right_saved_w = 260  # default chat panel width

        # remember user drags
        self.outer_split.splitterMoved.connect(self._on_outer_split_moved)
        self.inner_split.splitterMoved.connect(self._on_inner_split_moved)

        # Initial visibility states
        self._left_open = True
        self._right_open = False
        self.chat_panel.setVisible(self._right_open)  # ensure hidden at start
        self._apply_split_sizes(initial=True)

        # Status bar
        self.setStatusBar(QStatusBar(self))

        # One-time object names (so QSS can target cleanly)
        self.side_panel.setObjectName("SidePanel")
        self.chat_panel.setObjectName("ChatPanel")
        self.top_panel.setObjectName("TopPanel")
        self.chat_display.setObjectName("ChatDisplay")
        self.left_edge.setObjectName("EdgeBarLeft")
        self.right_edge.setObjectName("EdgeBarRight")

    def _apply_prefs(self, prefs):
        # theme
        self._variant = prefs.theme_variant
        self._apply_theme_variant()
        # spellcheck
        self._spell_enabled = prefs.spellcheck_enabled
        self._spell_locale = prefs.locale
        self.chat_display.input.set_spell_enabled(self._spell_enabled)
        self.chat_display.input.set_spell_locale(self._spell_locale)

    def _wire_signals(self):
        self.side_panel.sig_open_form.connect(self._open_test_form)
        self.top_panel.sig_closed.connect(self._on_top_closed)

        self.side_panel.create_conversation.connect(self._new_chat)
        self.side_panel.open_user_settings.connect(self._open_test_form)   # placeholder
        self.side_panel.open_memory_view.connect(self._open_test_form)     # placeholder
        self.side_panel.open_theme_manager.connect(self._open_test_form)   # placeholder

        # Open a saved conversation when the user activates a chat item
        self.side_panel.open_conversation.connect(self._open_conversation)
        self.side_panel.rename_conversation.connect(self._rename_conversation)
        self.side_panel.delete_conversation.connect(self._delete_conversation)

        self.side_panel.request_login.connect(self._open_login_flow)
        self.side_panel.request_logout.connect(self._do_logout)
        self.chat_display.bubbleAction.connect(self._on_bubble_action)

    def _init_theme(self):
        # Load + merge defaults (writes back if needed) then apply chosen variant
        themes_dir = Path("settings/themes")
        theme = ensure_theme(themes_dir)
        self._theme = theme
        self._apply_theme_variant()  # uses self._variant

    def _apply_theme_variant(self):
        app = QApplication.instance()
        colors = select_variant(self._theme, self._variant)
        apply_theme(app, self, colors)

        qml_tokens = export_qml_tokens(colors)
        self.chat_display.set_qml_tokens(qml_tokens)

    def set_theme(self, theme_dict: dict, variant: str | None = None):
        """
        Optional public setter if you want to inject user theme after login.
        """
        self._theme = theme_dict
        if variant:
            self._variant = variant
        self._apply_theme_variant()
        # Sync the menu toggle if it exists
        if hasattr(self, "act_dark_mode"):
            self.act_dark_mode.setChecked(self._variant == "dark")

    def _toggle_dark_mode(self, checked: bool):
        self._variant = "dark" if checked else "light"
        self._apply_theme_variant()

    def set_models_available(self, count: Optional[int]) -> None:
        self._models_available = count; self._refresh_status()

    def _refresh_status(self) -> None:
        parts = [f"Mode: {self.runtime_mode.upper()}"]
        if self.runtime_mode == "snout" and self.server_url:
            parts.append(f"Server: {self.server_url}")
        if self._db_mode:
            parts.append(f"DB: {self._db_mode}")

        # NEW: show current model
        model_id = getattr(self.session.current.prefs, "model_id", None)
        if model_id:
            parts.append(f"Model: {model_id}")
            try:
                parts.append("Modality: " + ("👁️‍🗨️" if self.session.current.vision else "💬"))
            except Exception:
                pass

        if isinstance(self._models_available, int):
            parts.append(f"Models: {self._models_available}")
        self.statusBar().showMessage(" | ".join(parts))

    def _on_conversation_started(self, conv_id: int):
        """
        Called when ChatController creates a new saved_conversations row
        for the current user chat.
        """
        # Refresh 'My Chats' + highlight this conversation
        self.side_panel.refresh_chats()
        try:
            self.side_panel.set_active_chat(conv_id)
        except Exception:
            pass
        # Update right-hand panel status + ID badge
        try:
            self.chat_panel.set_conversation_saved(conv_id)
            title = self._get_conversation_title(conv_id)
            self.chat_panel.set_conversation_title(title)
        except Exception:
            pass

    def toggle_left_panel(self):
        self._left_open = not self._left_open
        # show/hide the actual widget
        self.side_panel.setVisible(self._left_open)
        self._apply_split_sizes()

    def toggle_right_panel(self):
        self._right_open = not self._right_open
        self.chat_panel.setVisible(self._right_open)
        self._apply_split_sizes()

    def _apply_split_sizes(self, initial: bool = False):
        # left container width = edge (always) + panel (if visible)
        left_panel_w = (self._left_saved_w if self._left_open else 0)
        left_w = EDGE_WIDTH + left_panel_w

        # right container width = panel (if visible) + edge (always)
        right_panel_w = (self._right_saved_w if self._right_open else 0)
        right_w = right_panel_w + EDGE_WIDTH

        # Outer: [ left_container | chat_area ]
        outer_total = max(1100, self.width())
        chat_area_w = max(800, outer_total - left_w)
        self.outer_split.setSizes([left_w, chat_area_w])

        # Inner: [ chat_display | right_container ]
        inner_total = chat_area_w
        center_w = max(700, inner_total - right_w)
        self.inner_split.setSizes([center_w, right_w])

    def _on_outer_split_moved(self, pos: int, index: int):
        # Only update saved left width when the panel is open.
        if not self._left_open:
            return
        left_container_w = self.outer_split.sizes()[0]
        # panel width = container minus edge bar
        self._left_saved_w = max(0, left_container_w - EDGE_WIDTH)

    def _on_inner_split_moved(self, pos: int, index: int):
        # Only update saved right width when open.
        if not self._right_open:
            return
        right_container_w = self.inner_split.sizes()[1]
        # panel width = container minus edge bar
        self._right_saved_w = max(0, right_container_w - EDGE_WIDTH)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._apply_split_sizes()

    # -------- NEW auth handlers --------
    def _open_login_flow(self):
        from .widgets.login_form import LoginForm
        admin: bool = self.session.has_admin()
        require_approval = self.session.signup_requires_approval()  # ← session, not file
        # This should be from the session data
        form = LoginForm(admin=admin, signup_requires_approval=require_approval)
        form.sig_close.connect(self.top_panel.close_panel)
        form.submit_admin_setup.connect(self._create_admin)
        form.submit_login.connect(self._login_user)
        form.submit_signup.connect(self._signup_user)
        self.top_panel.open_with(form)

    def _create_admin(self, username: str, password: str):
        from hamchat.db_ops import create_user

        # 1) Write to DB (single source of truth)
        try:
            uid = create_user(
                self._db,
                name=username,
                handle=username.lower(),
                email=None,
                username=username,
                password=password,
                role="admin",
            )
        except Exception as e:
            QMessageBox.critical(self, "Admin setup failed", f"{e}")
            return

        # 2) Reflect in-memory session (emits sessionChanged; updates UI immediately)
        self.session.mark_has_admin(True)  # persists via Settings; no restart needed
        self.session.load_user(uid, username, "admin", {})

        # 3) (Optional) Belt & braces: mirror to app.json too
        try:
            cfg = _load_app_cfg()
            cfg.setdefault("auth", {})["has_admin"] = True
            _save_app_cfg(cfg)
        except Exception as e:
            # Non-fatal: DB+session are already correct
            print(f"Warning: failed to write app.json has_admin flag: {e}")

        self.top_panel.close_panel()

    def _login_user(self, username: str, password: str):
        from hamchat.db_ops import authenticate
        result = authenticate(self._db, username=username, password=password)
        if not result:
            # ToDo: Show a QMessageBox here for UX feedback
            return
        uid, role, prefs = result
        self.session.load_user(uid, username, role, prefs or {})
        if self._new_chat(system_call=True):
            # ToDo: Active conversation needs to be stored with this uid
            pass
        self.top_panel.close_panel()

    def _signup_user(self, username: str, password: str):
        if self.session.signup_requires_approval():
            from hamchat.db_ops import submit_signup_request
            rid = submit_signup_request(
                self._db,
                name=username,
                handle=username.lower(),
                username=username,
                email=None,
                password=password,
            )
            QMessageBox.information(
                self, "Signup request submitted",
                f"Request #{rid} has been sent. An admin will need to approve it before you can log in."
            )
            self.top_panel.close_panel()
            return

        # self-serve path (unchanged)
        from hamchat.db_ops import create_user
        uid = create_user(
            self._db,
            name=username, handle=username.lower(), email=None,
            username=username, password=password, role="user"
        )
        self.session.load_user(uid, username, "user", {})
        self.top_panel.close_panel()

    def _do_logout(self):
        self.session.logout()
        self._new_chat()

    # ----------------- Settings helpers -----------------

    def _open_test_form(self):
        form = TestForm(); form.sig_close.connect(self.top_panel.close_panel); self.top_panel.open_with(form)

    def _on_top_closed(self): pass

    def _on_model_changed_from_menu(self, model_id: str) -> None:
        # 1) Update session + persist
        self.session.set_model_id(model_id)

        # 2) Tell the ChatController if it supports dynamic model switching
        if hasattr(self, "chat_controller") and hasattr(self.chat_controller, "set_model_name"):
            self.chat_controller.set_model_name(model_id)

        # 3) Refresh status bar text
        self._refresh_status()

    def _open_model_manager(self):
        from .widgets.model_manager import ModelManagerForm
        models = self.session.get_model_choices()
        current = self.session.get_model_id()
        form = ModelManagerForm(models=models, current_model=current)
        form.sig_close.connect(self.top_panel.close_panel)
        self.top_panel.open_with(form)

    # ----------------- Chat helpers -----------------

    def _on_bubble_action(self, action, index, role, text):
        # Guard rails: confirm destructive actions before touching history
        if action in ("edit_resend", "resend", "regenerate"):
            if not self._confirm_bubble_action(action, role):
                return

        if action == "edit_resend":
            if not hasattr(self, "chat_controller"):
                return
            try:
                payload = self.chat_controller.prepare_edit_resend(index)
            except Exception:
                payload = None
            if not payload:
                return

            # Pre-fill the input and pending attachments
            self.chat_display.input.setPlainText(payload.get("text") or "")
            try:
                self.chat_display.set_pending_attachments(payload.get("attachments") or [])
            except Exception:
                pass
            return

        if not hasattr(self, "chat_controller"):
            return

        if action == "resend":
            self.chat_controller.resend_message(index)
        elif action == "regenerate":
            self.chat_controller.regenerate_from(index)
        elif action == "fork":
            self.chat_controller.fork_chat_at(index)

    def _confirm_bubble_action(self, action: str, role: str) -> bool:
        """
        Ask the user to confirm destructive bubble actions like resend / edit-resend / regenerate.
        Returns True if the user confirms, False otherwise.
        """
        # Default: non-destructive → no prompt
        title: str
        text: str

        if action == "edit_resend":
            title = "Edit and resend this turn?"
            text = (
                "This will delete this message and all later messages in this chat, "
                "then move the text and any attachments back into the input so you can "
                "edit and send it again.\n\n"
                "The existing assistant reply and any later turns will be lost.\n\n"
                "Do you want to continue?"
            )
        elif action == "resend":
            title = "Resend this turn?"
            text = (
                "This will delete this message and all later messages in this chat, "
                "then send the same text again.\n\n"
                "The existing assistant reply and any later turns will be lost.\n\n"
                "Do you want to continue?"
            )
        elif action == "regenerate":
            title = "Regenerate this reply?"
            text = (
                "This will delete this assistant reply and all later messages in this chat, "
                "then ask the assistant to answer the same user message again.\n\n"
                "The existing reply and any later turns will be lost.\n\n"
                "Do you want to continue?"
            )
        else:
            # Non-destructive or unknown action → no confirmation
            return True

        resp = QMessageBox.question(
            self,
            title,
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return resp == QMessageBox.StandardButton.Yes

    def _rename_conversation(self, conv_id: int, new_title: str):
        from hamchat import db_ops as dbo
        if not self._db:
            return
        new_title = (new_title or "").strip()
        if not new_title:
            return
        try:
            dbo.rename_conversation(self._db, conversation_id=int(conv_id), title=new_title)
        except Exception as e:
            log.exception("rename_conversation failed: %s", e)
            QMessageBox.critical(self, "Rename failed", "Could not rename this chat.")
            return
        self.side_panel.refresh_chats()
        try:
            self.side_panel.set_active_chat(conv_id)
        except Exception:
            pass

        # If this conversation is currently open, update ChatPanel title too
        try:
            if hasattr(self.chat_controller, "current_conversation_id") and \
               self.chat_controller.current_conversation_id() == int(conv_id):
                self.chat_panel.set_conversation_title(new_title)
        except Exception:
            pass

    def _delete_conversation(self, conv_id: int):
        from hamchat import db_ops as dbo
        if not self._db:
            return
        try:
            dbo.delete_conversation(self._db, conversation_id=int(conv_id))
        except Exception as e:
            log.exception("delete_conversation failed: %s", e)
            QMessageBox.critical(self, "Delete failed", "Could not delete this chat from the database.")
            return

        # If we just deleted the open conversation, clear the view
        current_id = None
        try:
            if hasattr(self.chat_controller, "current_conversation_id"):
                current_id = self.chat_controller.current_conversation_id()
        except Exception:
            current_id = None

        if current_id == int(conv_id):
            self.chat_controller.reset_history()
            self.chat_display.clear_messages()
            self.chat_panel.on_new_chat_started()

        self.side_panel.refresh_chats()
        try:
            self.side_panel.set_active_chat(None)
        except Exception:
            pass

    def _load_user_chats(self):
        """
        Loader for SidePanel "My Chats".
        Returns a sequence of (conversation_id, label) tuples for the current user.
        Guests/admins → empty list.
        """
        from hamchat import db_ops as dbo

        if not self._db:
            return ()

        uid = getattr(self.session.current, "user_id", None)
        role = getattr(self.session.current, "role", "guest")
        if not uid or role != "user":
            return ()

        try:
            rows = dbo.list_conversations(self._db, user_id=int(uid), limit=50)
        except Exception as e:
            log.exception("list_conversations failed: %s", e)
            return ()

        items = []
        for r in rows:
            cid = int(r["id"])
            title = (r.get("title") or "").strip() or f"Chat {cid}"
            items.append((cid, title))
        return items

    def _get_conversation_title(self, conv_id: int) -> str:
        """Best-effort lookup of a conversation title from the DB."""
        from hamchat import db_ops as dbo
        if not self._db:
            return f"Chat {conv_id}"
        uid = getattr(self.session.current, "user_id", None)
        if not uid:
            return f"Chat {conv_id}"
        try:
            rows = dbo.list_conversations(self._db, user_id=int(uid), limit=200)
        except Exception:
            return f"Chat {conv_id}"
        for r in rows:
            if int(r["id"]) == int(conv_id):
                title = (r.get("title") or "").strip()
                return title or f"Chat {conv_id}"
        return f"Chat {conv_id}"

    def _open_conversation(self, conv_id: int):
        """
        Load an existing conversation from the DB into the chat display and controller.
        """
        from hamchat import db_ops as dbo
        if not self._db:
            return

        try:
            msgs = dbo.list_messages(self._db, conversation_id=int(conv_id), limit=200)
        except Exception as e:
            log.exception("list_messages failed: %s", e)
            QMessageBox.critical(self, "Load failed", "Could not load this chat from the database.")
            return

        # Clear current UI messages (but don't pop the confirmation dialog here)
        self.chat_display.clear_messages()

        # Fill the display from DB rows
        for m in msgs:
            sender = m.get("sender_type", "assistant")
            text = m.get("content", "") or ""
            if not text:
                continue

            if sender == "user":
                role = "user"
            elif sender == "system":
                role = "system"
            else:
                # treat tool/assistant as assistant in the UI
                role = "assistant"

            self.chat_display.append_message(role, text)

        # Tell the controller which conversation + history to use for future prompts
        try:
            self.chat_controller.load_conversation(int(conv_id), msgs)
        except Exception as e:
            log.exception("chat_controller.load_conversation failed: %s", e)

        # Update right-hand chat panel "Created" timestamp from first message
        if msgs:
            first_ts = msgs[0].get("created")
            try:
                from PyQt6.QtCore import QDateTime
                if isinstance(first_ts, (int, float)):
                    dt = QDateTime.fromSecsSinceEpoch(int(first_ts))
                    self.chat_panel.set_created_at(dt)
            except Exception:
                pass

        # This is a persisted conversation
        try:
            self.chat_panel.set_conversation_saved(conv_id)
            title = self._get_conversation_title(conv_id)
            self.chat_panel.set_conversation_title(title)
        except Exception:
            pass
        try:
            self.side_panel.set_active_chat(conv_id)
        except Exception:
            pass

    def _new_chat(self, system_call: bool=False) -> bool:
        """
        Start a brand-new chat. Returns True iff the current chat was cleared.

        Behavior:
          - If there are no *user* messages in the view, exit quietly (return False).
          - If the current conversation is already persisted in the DB, clear without warning.
          - Otherwise (ephemeral chat), prompt for confirmation before clearing.
        """
        # 1) Early exit if nothing from the user yet
        try:
            msgs = self.chat_display.export_messages()  # [{role,text}, …]
        except Exception:
            msgs = []
        has_user_msgs = any(
            (m.get("role") == "user" and (m.get("text") or "").strip())
            for m in msgs
        )
        if not has_user_msgs:
            return False

        # 2) If this chat is already saved in the DB, just clear it – nothing is lost
        is_persisted = False
        try:
            if hasattr(self, "chat_controller") and hasattr(self.chat_controller, "has_persisted_conversation"):
                is_persisted = self.chat_controller.has_persisted_conversation()
        except Exception:
            is_persisted = False

        if is_persisted:
            self.chat_controller.reset_history()
            self.chat_display.clear_messages()
            self.chat_panel.on_new_chat_started()
            try:
                self.side_panel.set_active_chat(None)
            except Exception:
                pass
            return True

        # 3) Ephemeral chat: confirm before throwing it away
        resp = QMessageBox.question(
            self,
            "Discard current chat?",
            "There are messages in the current chat. Start a new one and discard them?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if resp != QMessageBox.StandardButton.Yes:
            return False

        self.chat_controller.reset_history()
        self.chat_display.clear_messages()
        self.chat_panel.on_new_chat_started()
        try:
            self.side_panel.set_active_chat(None)
        except Exception:
            pass
        return True

    def _on_send_payload_from_ui(self, text: str, attachments: list):
        if not attachments:
            return  # shouldn't happen, but harmless

        if not getattr(self.session.current, "vision", False):
            # Fall back to plain text send (re-use existing wiring)
            self.statusBar().showMessage(
                f"Note: {self.session.get_model_id()} doesn’t support images; sending text only.", 4000
            )
            # Re-emit the text signal so the existing controller path handles it:
            try:
                self.chat_display.sig_send_text.emit(text)
            except Exception:
                # or call your controller's text-send method directly if you prefer
                pass
            return

        # Vision path: process + send with media
        try:
            from hamchat.media_helper import process_images
            batch = process_images(attachments, ephemeral=(self.session.current.role != "user"), db=self._db,
                                   session=self.session)
            parts = batch["llm_parts"]
            thumb_paths = [t["path"] for t in batch.get("thumbs", [])]
            attachments_meta = []
            for stored, thumb in zip(batch.get("stored", []), batch.get("thumbs", [])):
                attachments_meta.append({
                    "file_id": stored["file_id"],
                    "sha256": stored["sha256"],
                    "mime": stored["mime"],
                    "thumb_file_id": thumb.get("file_id"),
                    "thumb_sha256": thumb.get("sha256"),
                })
        except Exception as e:
            self.statusBar().showMessage(f"Attachment processing failed: {e}", 6000)
            # As a last resort, send text so the user isn't blocked
            self.chat_display.sig_send_text.emit(text)
            return

        if parts:
            if thumb_paths:
                self.chat_display.draw_thumbs(thumb_paths)
            self.chat_controller.send_user_with_media(text, parts, attachments_meta or None)
        else:
            self.chat_display.sig_send_text.emit(text)

    def closeEvent(self, ev):
        ok = True
        try:
            ctrl = getattr(self, "chat_controller", None)
            if ctrl is not None:
                ok = bool(ctrl.hard_kill())
        except Exception as e:
            log.exception("hard_kill failed: %s", e)
            ok = False

        super().closeEvent(ev)
