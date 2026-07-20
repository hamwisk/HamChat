# hamchat/ui/widgets/chat_panel.py
from __future__ import annotations
import json, logging
from typing import List, Dict, Any
from PyQt6.QtCore import Qt, QDateTime, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QPushButton, QToolButton, QFrame, QFormLayout,
    QFileDialog, QMessageBox, QHBoxLayout, QSizePolicy, QListWidget, QListWidgetItem,
    QAbstractItemView, QMenu
)

log = logging.getLogger("ui.chat_panel")

# Simple collapsible expander (same feel as SidePanel)
class Expander(QFrame):
    def __init__(self, title: str, parent=None, *, expanded: bool = True):
        super().__init__(parent)
        self.setObjectName("Expander")
        self._header = QToolButton(text=title, checkable=True, checked=expanded)
        self._header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._header.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self._header.toggled.connect(self._on_toggled)

        self._body = QWidget(self)
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(8, 6, 8, 6)
        self._body.setVisible(expanded)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._header)
        lay.addWidget(self._body)

    def _on_toggled(self, checked: bool):
        self._body.setVisible(checked)
        self._header.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)

    def set_content(self, w: QWidget):
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
        self._body_layout.addWidget(w)

class ChatPanel(QWidget):
    """
    Right-hand panel showing attachments (stub), conversation metadata, and export.
    Expects a ChatDisplay instance so we can read messages/updates.
    """
    attachmentOpenRequested = pyqtSignal(int)
    attachmentAttachRequested = pyqtSignal(int)
    attachmentScrollRequested = pyqtSignal(int)

    def __init__(self, parent=None, *, chat_display=None, attachments_loader=None):
        super().__init__(parent)
        self._cd = chat_display  # ChatDisplay
        self._created = QDateTime.currentDateTime()
        self._attachments: List[Dict[str, Any]] = []  # future use
        self._conv_id: int | None = None
        self._saved: bool = False
        self._title: str = ""
        self._attachments_loader = attachments_loader

        root = QVBoxLayout(self); root.setContentsMargins(12,12,12,12); root.setSpacing(10)

        # Header row with Export button
        hdr = QFrame(self)
        hl = QHBoxLayout(hdr); hl.setContentsMargins(0,0,0,0); hl.setSpacing(8)
        title = QLabel("<b>Chat Panel</b>", hdr)
        title.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        btn_export = QPushButton("Export chat (JSON)", hdr)
        btn_export.clicked.connect(self._on_export_clicked)
        hl.addWidget(title, 1); hl.addWidget(btn_export, 0)
        root.addWidget(hdr)

        # Attachments expander (UI only for now)
        att = Expander("Attachments", expanded=True)
        att_body = QWidget(att); att_lay = QVBoxLayout(att_body); att_lay.setContentsMargins(0,0,0,0)
        self._att_placeholder = QLabel("No attachments yet.")
        self._att_placeholder.setStyleSheet("color:#8b8f97; font-size:12px;")
        att_lay.addWidget(self._att_placeholder)
        self._att_list = QListWidget(att_body)
        self._att_list.setFrameShape(QFrame.Shape.NoFrame)
        self._att_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self._att_list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._att_list.setUniformItemSizes(True)
        self._att_list.setVisible(False)
        self._att_list.itemDoubleClicked.connect(self._on_attachment_item_activated)
        self._att_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._att_list.customContextMenuRequested.connect(self._on_attachment_context_menu)
        att_lay.addWidget(self._att_list)
        att.set_content(att_body)
        root.addWidget(att)

        # Metadata expander
        meta = Expander("Conversation details", expanded=True)
        meta_body = QWidget(meta); self._meta_form = QFormLayout(meta_body)
        self._meta_form.setContentsMargins(0,0,0,0)

        self._lbl_title = QLabel("")
        self._lbl_created = QLabel("")      # filled below
        self._lbl_count = QLabel("")        # message count
        self._lbl_status = QLabel("")       # Saved / Unsaved
        self._lbl_conv_id = QLabel("")      # numeric id or em dash
        self._meta_form.addRow("Title:", self._lbl_title)
        self._meta_form.addRow("Created:", self._lbl_created)
        self._meta_form.addRow("Messages:", self._lbl_count)
        self._meta_form.addRow("Status:", self._lbl_status)
        self._meta_form.addRow("Conversation ID:", self._lbl_conv_id)

        meta.set_content(meta_body)
        root.addWidget(meta)

        root.addStretch(1)

        # Initial fill + live updates
        self._refresh_meta()
        self._connect_model_signals()

    # -------- public hooks ----------
    def on_new_chat_started(self):
        """Call when a brand new conversation begins."""
        self._created = QDateTime.currentDateTime()
        self._attachments.clear()
        self._conv_id = None
        self._saved = False
        self._title = ""
        self._refresh_meta()

    def set_created_at(self, when: QDateTime):
        self._created = when
        self._refresh_meta()

    def set_conversation_title(self, title: str):
        """Set the human-readable title of this conversation."""
        self._title = (title or "").strip()
        self._refresh_meta()

    def set_conversation_saved(self, conv_id: int):
        """Mark this panel as attached to a persisted conversation."""
        self._conv_id = int(conv_id)
        self._saved = True
        self._refresh_meta()

    def set_conversation_unsaved(self):
        """Mark current chat as not yet persisted (e.g. new, guest, admin)."""
        self._conv_id = None
        self._saved = False
        self._refresh_meta()

    def set_attachments(self, rows: List[Dict[str, Any]]) -> None:
        """Update the attachments list UI from DB rows."""
        normalized: List[Dict[str, Any]] = []
        self._att_list.clear()

        for r in rows or []:
            try:
                row = dict(r)
            except Exception:
                if isinstance(r, dict):
                    row = r
                else:
                    continue
            normalized.append(row)
            name = row.get("original_name") or "(unnamed file)"
            ref_count = row.get("ref_count")
            label = name
            if isinstance(ref_count, (int, float)) and ref_count > 1:
                label = f"{name} (x{int(ref_count)})"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, row.get("file_id"))
            self._att_list.addItem(item)

        self._attachments = normalized
        if not normalized:
            self._att_placeholder.setVisible(True)
            self._att_list.setVisible(False)
        else:
            self._att_placeholder.setVisible(False)
            self._att_list.setVisible(True)

    # -------- internals ----------
    def _connect_model_signals(self):
        if not self._cd:
            return
        model = self._cd.message_model()
        # Refresh meta when messages list changes
        model.rowsInserted.connect(lambda *_: self._refresh_meta())
        model.rowsRemoved.connect(lambda *_: self._refresh_meta())
        model.modelReset.connect(lambda *_: self._refresh_meta())

    def _file_id_from_item(self, item: QListWidgetItem) -> int | None:
        if item is None:
            return None
        val = item.data(Qt.ItemDataRole.UserRole)
        try:
            return int(val)
        except Exception:
            return None

    def _on_attachment_item_activated(self, item: QListWidgetItem):
        file_id = self._file_id_from_item(item)
        if file_id is not None:
            self.attachmentOpenRequested.emit(int(file_id))

    def _on_attachment_context_menu(self, pos):
        item = self._att_list.itemAt(pos)
        file_id = self._file_id_from_item(item)
        if file_id is None:
            return

        menu = QMenu(self)
        act_open = menu.addAction("Open")
        act_attach = menu.addAction("Attach to prompt")
        act_scroll = menu.addAction("Scroll to message…")
        chosen = menu.exec(self._att_list.mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == act_open:
            self.attachmentOpenRequested.emit(int(file_id))
        elif chosen == act_attach:
            self.attachmentAttachRequested.emit(int(file_id))
        elif chosen == act_scroll:
            self.attachmentScrollRequested.emit(int(file_id))

    def _refresh_attachments_from_loader(self) -> None:
        """Ask the loader for attachments for the current conversation."""
        if not callable(self._attachments_loader) or self._conv_id is None:
            self.set_attachments([])
            return
        try:
            rows = self._attachments_loader(int(self._conv_id))
        except Exception:
            log.exception("Attachment loader failed for conversation %s", self._conv_id)
            self.set_attachments([])
            return
        try:
            self.set_attachments(rows or [])
        except Exception:
            self.set_attachments([])

    def _refresh_meta(self):
        self._lbl_created.setText(self._created.toString(Qt.DateFormat.ISODate))
        count = 0
        if self._cd:
            try:
                count = self._cd.message_count()
            except Exception:
                pass
        self._lbl_count.setText(str(count))
        self._lbl_status.setText("Saved" if self._saved and self._conv_id is not None else "Unsaved")
        self._lbl_conv_id.setText(str(self._conv_id) if self._conv_id is not None else "—")
        self._lbl_title.setText(self._title or "—")
        self._refresh_attachments_from_loader()

    def _on_export_clicked(self):
        include_attachments = False
        if self._attachments:
            resp = QMessageBox.question(
                self, "Export attachments?",
                "This chat has attachments. Do you want to export attachments too?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel
            )
            if resp == QMessageBox.StandardButton.Cancel:
                return
            include_attachments = (resp == QMessageBox.StandardButton.Yes)

        # Ask for destination
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Chat", "chat_export.json", "JSON Files (*.json)"
        )
        if not path:
            return

        # Build payload
        messages = []
        if self._cd:
            try:
                messages = self._cd.export_messages()
            except Exception:
                messages = []

        payload: Dict[str, Any] = {
            "created": self._created.toString(Qt.DateFormat.ISODate),
            "title": self._title,
            "messages": messages,
        }
        if include_attachments:
            try:
                payload["attachments"] = [dict(a) for a in self._attachments] if self._attachments else []
            except Exception:
                payload["attachments"] = []

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            QMessageBox.critical(self, "Export failed", f"Could not write file:\n{e}")
            return

        QMessageBox.information(self, "Export complete", f"Chat exported to:\n{path}")
