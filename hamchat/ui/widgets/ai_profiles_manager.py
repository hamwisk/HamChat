# ai_profiles_manager.py

from __future__ import annotations

import json
from typing import Optional, Dict, Any

from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QPoint
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QListWidget,
    QListWidgetItem,
    QLabel,
    QLineEdit,
    QTextEdit,
    QFormLayout,
    QSpacerItem,
    QSizePolicy,
    QPushButton,
    QMenu,
    QFileDialog,
    QMessageBox,
    QScrollArea,
    QComboBox,
    QSlider,
    QToolButton,
    QFrame,
)

from hamchat import db_ops as dbo
from hamchat import media_helper


class AIProfilesManager(QWidget):
    """
    Top-panel widget for managing AI profiles.
    Left: list of profiles (with star on the active one).
    Right: editable form with autosave + revert.
    """

    sig_close = pyqtSignal()
    sig_profile_activated = pyqtSignal(int)
    sig_profiles_changed = pyqtSignal()

    def __init__(self, conn, session_mgr, active_profile_id: Optional[int] = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._db = conn
        self._session = session_mgr
        self._active_profile_id = active_profile_id

        self._current_profile_id: Optional[int] = None
        self._current_profile_data: Dict[str, Any] = {}
        self._dirty: bool = False
        self._avatar_path: str = ""
        self._suppress_autosave: bool = False

        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.timeout.connect(self._autosave_now)

        self._build_ui()
        self._load_profiles()

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        root_layout = QHBoxLayout(self)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.setSpacing(12)

        # Left: profiles list
        self.profiles_list = QListWidget(self)
        self.profiles_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.profiles_list.setMinimumWidth(200)
        self.profiles_list.itemActivated.connect(self._on_profile_activated)
        self.profiles_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.profiles_list.customContextMenuRequested.connect(self._on_profiles_context_menu)

        # Right: details form container
        right_container = QWidget(self)
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        # --- Header: title + Close button --------------------------------
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)

        title_label = QLabel("AI Profiles Manager", right_container)
        title_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        title_label.setStyleSheet("font-weight: bold;")

        btn_close = QPushButton("Close", right_container)
        btn_close.clicked.connect(self.sig_close.emit)

        header_layout.addWidget(title_label)
        header_layout.addStretch(1)
        header_layout.addWidget(btn_close)

        # --- Form fields (scrollable) --------------------------------------------------
        scroll = QScrollArea(right_container)
        scroll.setWidgetResizable(True)
        content = QWidget(scroll)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(10)

        form_basic = QFormLayout()
        form_basic.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form_basic.setFormAlignment(Qt.AlignmentFlag.AlignTop)

        self.display_name_edit = QLineEdit(content)
        self.short_desc_edit = QLineEdit(content)

        avatar_container = QFrame(content)
        avatar_layout = QHBoxLayout(avatar_container)
        avatar_layout.setContentsMargins(0, 0, 0, 0)
        self.avatar_label = QLabel("No avatar", avatar_container)
        self.avatar_label.setFixedSize(64, 64)
        self.avatar_label.setFrameShape(QFrame.Shape.StyledPanel)
        self.avatar_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.avatar_btn = QPushButton("+", avatar_container)
        self.avatar_btn.setFixedSize(24, 24)
        self.avatar_btn.clicked.connect(self._choose_avatar)
        avatar_layout.addWidget(self.avatar_label)
        avatar_layout.addWidget(self.avatar_btn, 0, Qt.AlignmentFlag.AlignBottom)

        form_basic.addRow("Display name:", self.display_name_edit)
        form_basic.addRow("Short description:", self.short_desc_edit)
        form_basic.addRow("Avatar:", avatar_container)

        prompt_label = QLabel("System prompt:", content)
        self.system_prompt_edit = QTextEdit(content)
        self.system_prompt_edit.setPlaceholderText("System / profile prompt goes here...")
        self.system_prompt_edit.setMinimumHeight(120)
        self.system_prompt_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.advanced_toggle = QToolButton(content)
        self.advanced_toggle.setText("Advanced settings")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setChecked(False)
        self.advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.advanced_toggle.toggled.connect(self._on_advanced_toggled)

        self.advanced_widget = QWidget(content)
        self.advanced_widget.setVisible(False)
        adv_form = QFormLayout(self.advanced_widget)
        adv_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.default_model_combo = QComboBox(self.advanced_widget)
        self.temp_slider = QSlider(Qt.Orientation.Horizontal, self.advanced_widget)
        self.temp_slider.setRange(0, 100)
        self.top_p_slider = QSlider(Qt.Orientation.Horizontal, self.advanced_widget)
        self.top_p_slider.setRange(0, 100)
        self.max_tokens_edit = QLineEdit(self.advanced_widget)

        adv_form.addRow("Default model:", self.default_model_combo)
        adv_form.addRow("Temperature:", self.temp_slider)
        adv_form.addRow("Top-p:", self.top_p_slider)
        adv_form.addRow("Max tokens:", self.max_tokens_edit)

        content_layout.addLayout(form_basic)
        content_layout.addWidget(prompt_label)
        content_layout.addWidget(self.system_prompt_edit)
        content_layout.addWidget(self.advanced_toggle)
        content_layout.addWidget(self.advanced_widget)

        btn_row = QHBoxLayout()
        self.btn_revert = QPushButton("Revert", content)
        self.btn_revert.clicked.connect(self._revert_changes)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_revert)
        content_layout.addLayout(btn_row)
        content_layout.addStretch(1)

        scroll.setWidget(content)

        # Pack right side
        right_layout.addLayout(header_layout)
        right_layout.addWidget(scroll)

        # Assemble root layout
        root_layout.addWidget(self.profiles_list)
        root_layout.addWidget(right_container)
        root_layout.setStretch(0, 1)  # list narrower
        root_layout.setStretch(1, 3)  # form wider

        # Signals for autosave
        self.profiles_list.currentItemChanged.connect(self._on_profile_selected)
        self.display_name_edit.textChanged.connect(self._on_field_changed)
        self.short_desc_edit.textChanged.connect(self._on_field_changed)
        self.system_prompt_edit.textChanged.connect(self._on_field_changed)
        self.default_model_combo.currentIndexChanged.connect(self._on_field_changed)
        self.temp_slider.valueChanged.connect(self._on_field_changed)
        self.top_p_slider.valueChanged.connect(self._on_field_changed)
        self.max_tokens_edit.textChanged.connect(self._on_field_changed)

        self._populate_model_choices()

    # ------------------------------------------------------------------ #
    # Data loading
    # ------------------------------------------------------------------ #

    def _load_profiles(self) -> None:
        self.profiles_list.clear()
        if not self._db:
            return

        role = getattr(getattr(self._session, "current", None), "role", "guest")
        uid = getattr(getattr(self._session, "current", None), "user_id", None)
        include_builtin = True
        owner_id = None if role == "admin" else uid

        try:
            rows = dbo.list_ai_profiles(self._db,
                                        owner_user_id=owner_id,
                                        include_builtin=include_builtin)
        except Exception:
            rows = []

        selected_item = None

        # --- Synthetic "Default" entry ---
        default_label = "Default"
        default_item = QListWidgetItem(default_label)
        default_item.setData(Qt.ItemDataRole.UserRole, 0)
        default_item.setToolTip("Run the model with no special persona settings.")

        if self._active_profile_id is None or int(self._active_profile_id) == 0:
            default_item.setText(f"★ {default_label}")
            f = default_item.font()
            f.setBold(True)
            default_item.setFont(f)
            selected_item = default_item

        self.profiles_list.addItem(default_item)

        # --- Real profiles from DB ---
        for r in rows:
            pid = r.get("id")
            if pid is None:
                continue

            label = r.get("display_name") or f"Profile {pid}"
            short_desc = r.get("short_description") or ""
            display = label

            it = QListWidgetItem(display)
            it.setData(Qt.ItemDataRole.UserRole, int(pid))

            # Tooltip = short description, if present
            if short_desc:
                it.setToolTip(short_desc)

            if self._active_profile_id is not None and int(pid) == int(self._active_profile_id):
                it.setText(f"★ {display}")
                f = it.font()
                f.setBold(True)
                it.setFont(f)
                selected_item = it

            self.profiles_list.addItem(it)

        if selected_item:
            self.profiles_list.setCurrentItem(selected_item)
        elif self.profiles_list.count() > 0:
            self.profiles_list.setCurrentRow(0)

    # ------------------------------------------------------------------ #
    # Selection handling
    # ------------------------------------------------------------------ #

    def _on_profile_selected(self, current: Optional[QListWidgetItem], previous: Optional[QListWidgetItem]):
        if not current:
            self._clear_form()
            return
        pid = current.data(Qt.ItemDataRole.UserRole)
        if pid is None or not self._db:
            self._clear_form()
            return
        pid = int(pid)
        if pid == 0:
            # Synthetic default → clear form, no current DB profile
            self._suppress_autosave = True
            self._current_profile_id = None
            self._current_profile_data = {}
            self._dirty = False
            self._autosave_timer.stop()
            self._clear_form()
            self._suppress_autosave = False
            return
        try:
            profile = dbo.get_ai_profile(self._db, int(pid))
        except Exception:
            profile = None
        if not profile:
            self._clear_form()
            return

        self._suppress_autosave = True
        self._current_profile_id = int(pid)
        self._current_profile_data = dict(profile)
        self._dirty = False
        self._autosave_timer.stop()

        self.display_name_edit.setText(profile.get("display_name") or "")
        self.short_desc_edit.setText(profile.get("short_description") or "")
        self.system_prompt_edit.setPlainText(profile.get("system_prompt") or "")
        self.max_tokens_edit.setText("" if profile.get("max_tokens") is None else str(profile.get("max_tokens")))
        self._avatar_path = profile.get("avatar") or ""
        self._update_avatar_preview()

        model_id = profile.get("default_model_id")
        found = False
        for i in range(self.default_model_combo.count()):
            if self.default_model_combo.itemData(i) == model_id:
                self.default_model_combo.setCurrentIndex(i)
                found = True
                break
        if not found:
            self.default_model_combo.setCurrentIndex(0)

        temp = profile.get("temperature")
        if temp is None:
            temp_val = 70
        else:
            temp_val = max(0, min(100, int(float(temp) * 100)))
        self.temp_slider.setValue(temp_val)

        top_p = profile.get("top_p")
        if top_p is None:
            top_val = 90
        else:
            top_val = max(0, min(100, int(float(top_p) * 100)))
        self.top_p_slider.setValue(top_val)

        self._suppress_autosave = False

    # ------------------------------------------------------------------ #
    # Autosave
    # ------------------------------------------------------------------ #

    def _on_field_changed(self, *_):
        if not self._db or self._suppress_autosave:
            return
        self._dirty = True
        self._autosave_timer.start(1000)

    def _collect_form_data(self) -> Dict[str, Any]:
        def _int(val: str) -> Optional[int]:
            try:
                return int(val)
            except Exception:
                return None

        return {
            "display_name": self.display_name_edit.text().strip(),
            "short_description": self.short_desc_edit.text().strip(),
            "system_prompt": self.system_prompt_edit.toPlainText(),
            "avatar": self._avatar_path or "",
            "default_model_id": self.default_model_combo.currentData(),
            "temperature": float(self.temp_slider.value()) / 100.0,
            "top_p": float(self.top_p_slider.value()) / 100.0,
            "max_tokens": _int(self.max_tokens_edit.text().strip()),
        }

    def _autosave_now(self):
        if not self._dirty or not self._db or self._suppress_autosave:
            return

        data = self._collect_form_data()
        role = getattr(getattr(self._session, "current", None), "role", "guest")
        uid = getattr(getattr(self._session, "current", None), "user_id", None)
        is_admin = role == "admin"
        owner_id = None if is_admin else uid

        # Snapshot of the last-saved avatar path (may be empty/None).
        old_avatar = (self._current_profile_data or {}).get("avatar")

        try:
            if self._current_profile_id is None:
                # New profile: only save if it has a display name.
                if not data.get("display_name"):
                    return
                new_id = dbo.create_ai_profile(
                    self._db,
                    owner_user_id=owner_id,
                    internal_name=(data.get("display_name") or "profile").lower().replace(" ", "_"),
                    display_name=data.get("display_name"),
                    short_description=data.get("short_description") or "",
                    system_prompt=data.get("system_prompt") or "",
                    avatar=data.get("avatar") or "",
                    default_model_id=data.get("default_model_id"),
                    temperature=data.get("temperature"),
                    top_p=data.get("top_p"),
                    max_tokens=data.get("max_tokens"),
                    is_builtin=is_admin,
                )
                self._current_profile_id = new_id
            else:
                # Existing profile: update in place.
                dbo.update_ai_profile(self._db, int(self._current_profile_id), **data)

            # Update our in-memory snapshot AFTER a successful save.
            self._current_profile_data.update(data)
            self._dirty = False

            # Refresh the left-hand list without re-triggering form reload,
            # so the cursor/scroll position in the editors stays put.
            try:
                self.profiles_list.blockSignals(True)
            except Exception:
                pass

            self._load_profiles()
            self._select_profile(self._current_profile_id)

            try:
                self.profiles_list.blockSignals(False)
            except Exception:
                pass

            self.sig_profiles_changed.emit()

            # If the avatar actually changed, clean up the previous CAS entry.
            new_avatar = data.get("avatar") or ""
            if old_avatar and old_avatar != new_avatar:
                media_helper.cleanup_profile_avatar(self._db, old_avatar)


        except Exception:
            # Don't explode on autosave; worst case: user tries again.
            pass

    # ------------------------------------------------------------------ #
    # Context menu and actions
    # ------------------------------------------------------------------ #

    def _on_profiles_context_menu(self, pos: QPoint):
        item = self.profiles_list.itemAt(pos)
        menu = QMenu(self)
        act_new = menu.addAction("New")
        pid = item.data(Qt.ItemDataRole.UserRole) if item else None
        act_activate = act_clone = act_delete = act_export = None
        if item:
            if int(pid) == 0:
                act_activate = menu.addAction("Activate")
            else:
                act_activate = menu.addAction("Activate")
                act_clone = menu.addAction("Clone")
                act_delete = menu.addAction("Delete")
                menu.addSeparator()
                act_export = menu.addAction("Export")

        act_import = menu.addAction("Import")
        chosen = menu.exec(self.profiles_list.mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == act_new:
            self._new_profile()
        elif chosen == act_activate and item:
            self._on_profile_activated(item)
        elif chosen == act_clone and pid not in (None, 0):
            self._clone_profile(int(pid))
        elif chosen == act_delete and pid not in (None, 0):
            self._delete_profile(int(pid))
        elif chosen == act_export and pid not in (None, 0):
            self._export_profile(int(pid))
        elif chosen == act_import:
            self._import_profile()

    def _on_profile_activated(self, item: QListWidgetItem):
        if not item:
            return
        pid = item.data(Qt.ItemDataRole.UserRole)
        if pid is None:
            return
        self._active_profile_id = int(pid)
        self.sig_profile_activated.emit(int(pid))
        self._load_profiles()

    def _new_profile(self):
        role = getattr(getattr(self._session, "current", None), "role", "guest")
        uid = getattr(getattr(self._session, "current", None), "user_id", None)
        is_admin = role == "admin"
        owner_id = None if is_admin else uid
        placeholder = "New profile"
        try:
            pid = dbo.create_ai_profile(
                self._db,
                owner_user_id=owner_id,
                internal_name=placeholder.lower().replace(" ", "_"),
                display_name=placeholder,
                short_description="",
                system_prompt="",
                is_builtin=is_admin,
            )
            self._load_profiles()
            self._select_profile(pid)
            self.sig_profiles_changed.emit()
        except Exception:
            pass

    def _select_profile(self, profile_id: Optional[int]):
        if profile_id is None:
            return
        for i in range(self.profiles_list.count()):
            it = self.profiles_list.item(i)
            pid = it.data(Qt.ItemDataRole.UserRole)
            try:
                pid = int(pid)
            except Exception:
                pid = None
            if pid == profile_id:
                self.profiles_list.setCurrentItem(it)
                return

    def _clone_profile(self, profile_id: int):
        try:
            data = dbo.get_ai_profile(self._db, int(profile_id)) or {}
        except Exception:
            data = {}
        if not data:
            return
        data = dict(data)
        data["display_name"] = f"{data.get('display_name','Profile')} (copy)"
        data["internal_name"] = f"{data.get('internal_name','profile')}_copy"
        role = getattr(getattr(self._session, "current", None), "role", "guest")
        uid = getattr(getattr(self._session, "current", None), "user_id", None)
        is_admin = role == "admin"
        owner_id = None if is_admin else uid
        try:
            pid = dbo.create_ai_profile(
                self._db,
                owner_user_id=owner_id,
                internal_name=data.get("internal_name"),
                display_name=data.get("display_name"),
                short_description=data.get("short_description") or "",
                avatar=data.get("avatar") or "",
                system_prompt=data.get("system_prompt") or "",
                allowed_models=data.get("allowed_models"),
                default_model_id=data.get("default_model_id"),
                temperature=data.get("temperature"),
                top_p=data.get("top_p"),
                max_tokens=data.get("max_tokens"),
                is_builtin=is_admin,
            )
            self._load_profiles()
            self._select_profile(pid)
            self.sig_profiles_changed.emit()
        except Exception:
            pass

    def _delete_profile(self, profile_id: int):
        try:
            prof = dbo.get_ai_profile(self._db, int(profile_id))
        except Exception:
            prof = None
        if not prof:
            return
        if prof.get("is_default"):
            QMessageBox.information(self, "Protected profile", "The default profile cannot be deleted.")
            return
        role = getattr(getattr(self._session, "current", None), "role", "guest")
        if prof.get("is_builtin") and role != "admin":
            QMessageBox.information(self, "Protected profile", "Only admins can delete shared profiles.")
            return
        resp = QMessageBox.question(
            self,
            "Delete profile?",
            f"Delete '{prof.get('display_name')}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if resp != QMessageBox.StandardButton.Yes:
            return
        try:
            dbo.delete_ai_profile(self._db, int(profile_id))
            if self._active_profile_id == profile_id:
                self._active_profile_id = None
            self._current_profile_id = None
            self._current_profile_data = {}
            self._load_profiles()
            self._clear_form()
            self.sig_profiles_changed.emit()
        except Exception:
            pass

    def _export_profile(self, profile_id: int):
        path, _ = QFileDialog.getSaveFileName(self, "Export profile", "profile.json", "JSON Files (*.json)")
        if not path:
            return
        try:
            data = dbo.export_ai_profile(self._db, int(profile_id))
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            QMessageBox.critical(self, "Export failed", f"Could not export profile:\n{e}")

    def _import_profile(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import profile", "", "JSON Files (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            QMessageBox.critical(self, "Import failed", f"Could not read file:\n{e}")
            return
        role = getattr(getattr(self._session, "current", None), "role", "guest")
        uid = getattr(getattr(self._session, "current", None), "user_id", None)
        is_admin = role == "admin"
        owner_id = None if is_admin else uid
        try:
            pid = dbo.import_ai_profile(self._db, owner_user_id=owner_id, data=data, is_builtin=is_admin)
            self._load_profiles()
            self._select_profile(pid)
            self.sig_profiles_changed.emit()
        except Exception as e:
            QMessageBox.critical(self, "Import failed", f"Could not import profile:\n{e}")

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _revert_changes(self):
        if not self._current_profile_data:
            return
        self._suppress_autosave = True
        self._autosave_timer.stop()
        data = self._current_profile_data
        self._dirty = False
        self.display_name_edit.setText(data.get("display_name") or "")
        self.short_desc_edit.setText(data.get("short_description") or "")
        self.system_prompt_edit.setPlainText(data.get("system_prompt") or "")
        self.max_tokens_edit.setText("" if data.get("max_tokens") is None else str(data.get("max_tokens")))
        self._avatar_path = data.get("avatar") or ""
        self._update_avatar_preview()

        model_id = data.get("default_model_id")
        found = False
        for i in range(self.default_model_combo.count()):
            if self.default_model_combo.itemData(i) == model_id:
                self.default_model_combo.setCurrentIndex(i)
                found = True
                break
        if not found:
            self.default_model_combo.setCurrentIndex(0)

        temp = data.get("temperature")
        self.temp_slider.setValue(70 if temp is None else max(0, min(100, int(float(temp) * 100))))
        top_p = data.get("top_p")
        self.top_p_slider.setValue(90 if top_p is None else max(0, min(100, int(float(top_p) * 100))))
        self._suppress_autosave = False

    def _clear_form(self) -> None:
        self._current_profile_id = None
        self._current_profile_data = {}
        self._dirty = False
        self._autosave_timer.stop()
        self.display_name_edit.clear()
        self.short_desc_edit.clear()
        self.system_prompt_edit.clear()
        self.max_tokens_edit.clear()
        self._avatar_path = ""
        self._update_avatar_preview()
        self.default_model_combo.setCurrentIndex(0)
        self.temp_slider.setValue(70)
        self.top_p_slider.setValue(90)

    def _choose_avatar(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select avatar",
            "",
            "Image Files (*.png *.jpg *.jpeg *.bmp *.webp)",
        )
        if not path:
            return

        # Push into CAS and normalise to a square thumb inside HamChat's data dir.
        stored_path = path
        if self._db is not None:
            try:
                # Use a smaller avatar size than the 96px chat thumbnails.
                stored_path = media_helper.store_profile_avatar(path, db=self._db, size=64)
            except Exception:
                # Fail soft – keep original path rather than crashing the form.
                stored_path = path

        self._avatar_path = stored_path
        self._update_avatar_preview()
        self._on_field_changed()

    def _update_avatar_preview(self):
        if self._avatar_path:
            pix = QPixmap(self._avatar_path)
            if not pix.isNull():
                self.avatar_label.setPixmap(pix.scaled(self.avatar_label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                self.avatar_label.setText("")
                return
        self.avatar_label.setPixmap(QPixmap())
        self.avatar_label.setText("No avatar")

    def _on_advanced_toggled(self, checked: bool):
        self.advanced_widget.setVisible(checked)
        self.advanced_toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)

    def _populate_model_choices(self):
        self.default_model_combo.clear()
        self.default_model_combo.addItem("— no default —", None)
        choices = []
        try:
            if hasattr(self._session, "get_model_choices"):
                choices = self._session.get_model_choices()
        except Exception:
            choices = []
        for mid, label in choices or []:
            self.default_model_combo.addItem(label or mid, mid)
