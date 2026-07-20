# hamchat/ui/widgets/side_panel.py
from __future__ import annotations
from typing import Callable, Optional, Sequence, Tuple
import logging
from PyQt6.QtCore import Qt, QPoint, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget, QListWidgetItem,
    QFrame, QToolButton, QSizePolicy, QLineEdit, QSpacerItem, QMenu, QInputDialog, QMessageBox
)

log = logging.getLogger("ui.side")

Item = Tuple[int, str]
Loader = Callable[[], Sequence[Item]]

def _empty_loader() -> Sequence[Item]:
    return ()

class Expander(QFrame):
    """
    Minimal collapsible section (title row + content area).
    """
    def __init__(self, title: str, parent=None, *, expanded: bool = True):
        super().__init__(parent)
        self.setObjectName("Expander")

        self.header = QToolButton(text=title, checkable=True, checked=expanded)
        self.header.setObjectName("ExpanderHeader")
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.header.toggled.connect(self._on_toggled)

        self.body = QWidget()
        self.body.setObjectName("ExpanderBody")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(8, 6, 8, 6)
        self.body.setVisible(expanded)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.header)
        lay.addWidget(self.body)

    def _on_toggled(self, checked: bool):
        self.body.setVisible(checked)
        self.header.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)

    def set_content(self, widget: QWidget):
        while self.body_layout.count():
            item = self.body_layout.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
        self.body_layout.addWidget(widget)


class SidePanel(QWidget):
    """
    Side rail with collapsible sections + simple search.
    Loaders are optional; if not provided, sections still render and can be filled later.
    """
    # Kept from your MVP:
    sig_open_form = pyqtSignal()

    # NEW: auth intents
    request_login = pyqtSignal()
    request_logout = pyqtSignal()

    # New intents (wire up as you add features)
    ai_profiles_manager = pyqtSignal()
    open_profile = pyqtSignal(int)
    profile_activated = pyqtSignal(int)
    create_conversation = pyqtSignal()
    open_conversation = pyqtSignal(int)
    open_memory_view = pyqtSignal()
    open_user_settings = pyqtSignal()
    open_theme_manager = pyqtSignal()
    rename_conversation = pyqtSignal(int, str)
    delete_conversation = pyqtSignal(int)

    # Admin-only intents
    open_admin_user = pyqtSignal(int)   # open user overview in top panel
    open_audit_view = pyqtSignal()      # open audit log / events view

    def __init__(
        self,
        parent=None,
        *,
        list_profiles: Optional[Callable[[], Sequence[Item]]] = None,
        list_chats: Optional[Callable[[], Sequence[Item]]] = None,
        list_users: Optional[Callable[[], Sequence[Item]]] = None,
    ):
        super().__init__(parent)
        self._list_profiles = list_profiles or _empty_loader
        self._list_chats = list_chats or _empty_loader
        self._list_users = list_users or _empty_loader

        self._prof_list: Optional[QListWidget] = None
        self._chat_list: Optional[QListWidget] = None
        self._user_list: Optional[QListWidget] = None

        self._session = None           # NEW
        self._name_lbl = None          # NEW
        self._btn_auth = None          # NEW

        self._active_chat_id: Optional[int] = None
        self._active_profile_id: Optional[int] = None

        # Keep handles to expanders so we can enable/disable/show/hide by role
        self._exp_profiles = None
        self._exp_chats = None
        self._exp_mem = None
        self._exp_settings = None
        self._exp_admin_users = None
        self._exp_admin_audit = None

        self._build()

    def _build(self):
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # Header
        hdr = QWidget(self)
        h = QVBoxLayout(hdr)
        h.setContentsMargins(0, 0, 0, 0)
        title = QLabel("<b>HamChat</b>")
        self._name_lbl = QLabel("Guest")
        self._name_lbl.setStyleSheet("color:#8b8f97; font-size:11px;")
        h.addWidget(title), h.addWidget(self._name_lbl)
        root.addWidget(hdr)

        # Search (filters list sections)
        self._search = QLineEdit(self, placeholderText="Search…")
        self._search.textChanged.connect(self._apply_filter)
        root.addWidget(self._search)

        # Quick actions
        quick = Expander("Quick actions", expanded=True)
        qa = QWidget(); ql = QVBoxLayout(qa); ql.setContentsMargins(0, 0, 0, 0)
        btn_new_chat = QPushButton("New chat"); btn_new_chat.clicked.connect(self.create_conversation.emit)

        # CHANGED: dynamic Login/Logout button
        self._btn_auth = QPushButton("Login")
        self._btn_auth.clicked.connect(self._on_auth_clicked)

        ql.addWidget(btn_new_chat)
        ql.addWidget(self._btn_auth)
        quick.set_content(qa)
        root.addWidget(quick)

        # Profiles
        profs = Expander("My AI Profiles", expanded=True)
        self._exp_profiles = profs
        self._prof_list = QListWidget()
        self._prof_list.itemActivated.connect(
            lambda it: self.profile_activated.emit(int(it.data(Qt.ItemDataRole.UserRole)))
        )
        self._prof_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._prof_list.customContextMenuRequested.connect(self._on_profiles_ctx_menu)

        btn_new_prof = QPushButton("AI-Profiles Manager")
        btn_new_prof.clicked.connect(self.ai_profiles_manager.emit)

        pf_wrap = QWidget(); pfl = QVBoxLayout(pf_wrap); pfl.setContentsMargins(0, 0, 0, 0)
        pfl.addWidget(self._prof_list); pfl.addWidget(btn_new_prof)
        profs.set_content(pf_wrap)
        root.addWidget(profs)

        # Chats
        chats = Expander("My Chats", expanded=True)
        self._exp_chats = chats
        self._chat_list = QListWidget()
        self._chat_list.itemActivated.connect(
            lambda it: self.open_conversation.emit(int(it.data(Qt.ItemDataRole.UserRole)))
        )
        self._chat_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._chat_list.customContextMenuRequested.connect(self._on_chats_ctx_menu)
        btn_new_chat2 = QPushButton("New chat")
        btn_new_chat2.clicked.connect(self.create_conversation.emit)

        ch_wrap = QWidget(); chl = QVBoxLayout(ch_wrap); chl.setContentsMargins(0, 0, 0, 0)
        chl.addWidget(self._chat_list); chl.addWidget(btn_new_chat2)
        chats.set_content(ch_wrap)
        root.addWidget(chats)

        # Memory
        mem = Expander("Memory", expanded=False)
        self._exp_mem = mem
        mem_btn = QPushButton("Open memory viewer")
        mem_btn.clicked.connect(self.open_memory_view.emit)
        mw = QWidget(); ml = QVBoxLayout(mw); ml.setContentsMargins(0, 0, 0, 0)
        ml.addWidget(mem_btn)
        mem.set_content(mw)
        root.addWidget(mem)

        # Settings
        sett = Expander("Settings", expanded=False)
        self._exp_settings = sett
        s_btn_user = QPushButton("User settings")
        s_btn_user.clicked.connect(self.open_user_settings.emit)
        s_btn_theme = QPushButton("Theme")
        s_btn_theme.clicked.connect(self.open_theme_manager.emit)
        sw = QWidget(); sl = QVBoxLayout(sw); sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(s_btn_user); sl.addWidget(s_btn_theme)
        sett.set_content(sw)
        root.addWidget(sett)
        self._exp_settings = sett

        # Admin: Users (visible only for admin role)
        admin_users = Expander("Users", expanded=False)
        self._user_list = QListWidget()
        self._user_list.itemActivated.connect(
            lambda it: self.open_admin_user.emit(int(it.data(Qt.ItemDataRole.UserRole)))
        )
        uw = QWidget();
        ul = QVBoxLayout(uw);
        ul.setContentsMargins(0, 0, 0, 0)
        ul.addWidget(self._user_list)
        admin_users.set_content(uw)
        root.addWidget(admin_users)
        self._exp_admin_users = admin_users

        # Admin: Audit
        admin_audit = Expander("Audit", expanded=False)
        audit_btn = QPushButton("Open audit view")
        audit_btn.clicked.connect(self.open_audit_view.emit)
        aw = QWidget();
        al = QVBoxLayout(aw);
        al.setContentsMargins(0, 0, 0, 0)
        al.addWidget(audit_btn)
        admin_audit.set_content(aw)
        root.addWidget(admin_audit)
        self._exp_admin_audit = admin_audit

        # Admin sections start hidden (non-admin roles)
        admin_users.hide()
        admin_audit.hide()

        # Spacer
        root.addItem(QSpacerItem(0, 0, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding))

        # Initial fills
        self.refresh_profiles()
        self.refresh_chats()

        # ---------- NEW: session binding ----------

    def _apply_role_mode(self, role: str):
        """
        Adjust which sections are enabled/visible based on session role.
        - guest:     everything visible but disabled, except Quick actions.
        - user:      profiles/chats/memory/settings enabled; admin sections hidden.
        - admin:     admin Users/Audit enabled; user-specific sections hidden.
        """
        role = (role or "guest").lower()
        is_guest = role == "guest"
        is_user = role == "user"
        is_admin = role == "admin"

        # My AI Profiles
        if self._exp_profiles:
            if is_guest:
                self._exp_profiles.setVisible(True)
                self._exp_profiles.setEnabled(False)
            else:
                self._exp_profiles.setVisible(True)
                self._exp_profiles.setEnabled(True)

        # My Chats / Memory
        for exp in (self._exp_chats, self._exp_mem):
            if not exp:
                continue
            if is_guest:
                exp.setVisible(True)
                exp.setEnabled(False)
            elif is_user:
                exp.setVisible(True)
                exp.setEnabled(True)
            elif is_admin:
                exp.setVisible(False)

        # Settings: visible but disabled for guest, enabled otherwise
        if self._exp_settings:
            if is_guest:
                self._exp_settings.setVisible(True)
                self._exp_settings.setEnabled(False)
            else:
                self._exp_settings.setVisible(True)
                self._exp_settings.setEnabled(True)

        # Admin sections: only for admin
        for exp in (self._exp_admin_users, self._exp_admin_audit):
            if not exp:
                continue
            if is_admin:
                exp.setVisible(True)
                exp.setEnabled(True)
            else:
                exp.setVisible(False)

    def bind_session(self, session_mgr):
        """Connect to SessionManager so header + button can reflect auth state."""
        self._session = session_mgr
        try:
            session_mgr.sessionChanged.connect(self._on_session_changed)
        except Exception:
            pass
        self._on_session_changed(getattr(session_mgr, "current", None))

    def _on_session_changed(self, state):
        try:
            username = getattr(state, "username", "Guest")
            user_id = getattr(state, "user_id", None)
            role = getattr(state, "role", "guest")
            profile_id = getattr(state, "profile_id", None)
        except Exception:
            username, user_id, role, profile_id = "Guest", None, "guest", None

        # Treat "no profile stored yet" as the synthetic Default (id 0)
        self._active_profile_id = 0 if profile_id is None else profile_id

        if self._name_lbl:
            self._name_lbl.setText(username or "Guest")
        if self._btn_auth:
            self._btn_auth.setText("Logout" if user_id else "Login")

        # Apply role-specific UI
        self._apply_role_mode(role)

        # Refresh role-dependent lists
        self.refresh_profiles()
        self.refresh_chats()
        self.refresh_users()

    def _on_auth_clicked(self):
        # Decide based on current session state
        user_id = None
        if self._session is not None:
            try:
                user_id = self._session.current.user_id
            except Exception:
                user_id = None
        if user_id:
            self.request_logout.emit()
        else:
            self.request_login.emit()

    # -------- public helpers --------
    def set_loaders(
        self,
        *,
        list_profiles: Optional[Callable[[], Sequence[Item]]] = None,
        list_chats: Optional[Callable[[], Sequence[Item]]] = None,
        list_users: Optional[Callable[[], Sequence[Item]]] = None,
    ):
        if list_profiles:
            self._list_profiles = list_profiles
        if list_chats:
            self._list_chats = list_chats
        if list_users:
            self._list_users = list_users
        self.refresh_profiles(); self.refresh_chats(); self.refresh_users()

    def set_active_chat(self, conv_id: Optional[int]):
        """Highlight the currently open conversation in the 'My Chats' list."""
        self._active_chat_id = conv_id
        if not self._chat_list:
            return
        lst = self._chat_list
        lst.blockSignals(True)

        # Clear all markers first
        for i in range(lst.count()):
            it = lst.item(i)
            label = it.text()
            if label.startswith("● "):
                label = label[2:]
                it.setText(label)
            f = it.font()
            f.setBold(False)
            it.setFont(f)

        lst.clearSelection()
        if conv_id is None:
            lst.blockSignals(False)
            return

        target_id = int(conv_id)
        for i in range(lst.count()):
            it = lst.item(i)
            if int(it.data(Qt.ItemDataRole.UserRole)) == target_id:
                label = it.text()
                if not label.startswith("● "):
                    it.setText(f"● {label}")
                f = it.font()
                f.setBold(True)
                it.setFont(f)
                lst.setCurrentItem(it)
                break

        lst.blockSignals(False)

    def refresh_profiles(self):
        self._fill_list(self._prof_list, self._list_profiles or _empty_loader)

    def refresh_chats(self):
        self._fill_list(self._chat_list, self._list_chats or _empty_loader)

    def refresh_users(self):
        self._fill_list(self._user_list, self._list_users or _empty_loader)

    # -------- internals --------
    def _fill_list(self, widget: Optional[QListWidget], loader: Loader):
        if widget is None:
            return
        widget.clear()
        try:
            items = list(loader() or ())
        except Exception as e:
            log.exception("List loader failed: %s", e)
            items = ()

        is_chat_list = (widget is self._chat_list)
        is_profile_list = (widget is self._prof_list)
        active_id = self._active_chat_id if is_chat_list else (self._active_profile_id if is_profile_list else None)
        selected_item = None

        for raw in items:
            # Support (id, label) or (id, label, tooltip)
            if len(raw) == 3:
                item_id, label, tooltip = raw
            else:
                item_id, label = raw
                tooltip = ""

            display = label
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, int(item_id))
            if tooltip:
                it.setToolTip(tooltip)
            if is_chat_list and active_id is not None and int(item_id) == int(active_id):
                # Mark active chat visually
                display = f"● {label}"
                f = it.font(); f.setBold(True); it.setFont(f)
                selected_item = it
            if is_profile_list and active_id is not None and int(item_id) == int(active_id):
                display = f"★ {label}"
                f = it.font(); f.setBold(True); it.setFont(f)
                selected_item = it
            it.setText(display)
            widget.addItem(it)
            if selected_item is not None:
                widget.setCurrentItem(selected_item)

    def set_active_profile(self, profile_id: Optional[int]) -> None:
        """Highlight the active AI profile."""
        self._active_profile_id = profile_id
        if not self._prof_list:
            return
        lst = self._prof_list
        lst.blockSignals(True)
        for i in range(lst.count()):
            it = lst.item(i)
            label = it.text()
            if label.startswith("★ "):
                it.setText(label[2:])
            f = it.font(); f.setBold(False); it.setFont(f)
            pid = it.data(Qt.ItemDataRole.UserRole)
            try:
                pid = int(pid)
            except Exception:
                pid = None
            if profile_id is not None and pid is not None and int(pid) == int(profile_id):
                if not it.text().startswith("★ "):
                    it.setText(f"★ {it.text()}")
                f = it.font(); f.setBold(True); it.setFont(f)
                lst.setCurrentItem(it)
        lst.blockSignals(False)

    def _apply_filter(self, text: str):
        text = (text or "").lower().strip()
        for lst in (self._prof_list, self._chat_list, self._user_list):
            if not lst:
                continue
            for i in range(lst.count()):
                it = lst.item(i)
                it.setHidden(text not in it.text().lower())

    def _on_profiles_ctx_menu(self, pos: QPoint):
        if not self._prof_list:
            return
        item = self._prof_list.itemAt(pos)
        if not item:
            return
        menu = QMenu(self)
        act_use = QAction("Set as active", self)
        pid = item.data(Qt.ItemDataRole.UserRole)
        if pid is not None:
            act_use.triggered.connect(lambda: self.profile_activated.emit(int(pid)))
        menu.addAction(act_use)
        menu.exec(self._prof_list.mapToGlobal(pos))

    def _on_chats_ctx_menu(self, pos: QPoint):
        if not self._chat_list:
            return
        item = self._chat_list.itemAt(pos)
        if not item:
            return
        conv_id = int(item.data(Qt.ItemDataRole.UserRole))
        menu = QMenu(self)

        act_open = QAction("Open", self)
        act_open.triggered.connect(lambda: self.open_conversation.emit(conv_id))
        menu.addAction(act_open)

        act_rename = QAction("Rename…", self)
        def do_rename():
            current = item.text()
            if current.startswith("● "):
                current = current[2:]
            new_title, ok = QInputDialog.getText(self, "Rename chat", "New title:", text=current)
            if ok:
                new_title = new_title.strip()
                if new_title:
                    self.rename_conversation.emit(conv_id, new_title)
        act_rename.triggered.connect(do_rename)
        menu.addAction(act_rename)

        act_delete = QAction("Delete…", self)
        def do_delete():
            resp = QMessageBox.question(
                self,
                "Delete chat?",
                f"Delete '{item.text()}'?\nThis cannot be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if resp == QMessageBox.StandardButton.Yes:
                self.delete_conversation.emit(conv_id)
        act_delete.triggered.connect(do_delete)
        menu.addAction(act_delete)

        menu.exec(self._chat_list.mapToGlobal(pos))
