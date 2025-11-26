# hamchat/ui/widgets/chat_panel.py
from __future__ import annotations
import json
from typing import List, Dict, Any
from PyQt6.QtCore import Qt, QDateTime
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QPushButton, QToolButton, QFrame, QFormLayout,
    QFileDialog, QMessageBox, QHBoxLayout, QSizePolicy
)

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
    def __init__(self, parent=None, *, chat_display=None):
        super().__init__(parent)
        self._cd = chat_display  # ChatDisplay
        self._created = QDateTime.currentDateTime()
        self._attachments: List[Dict[str, Any]] = []  # future use
        self._conv_id: int | None = None
        self._saved: bool = False
        self._title: str = ""

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

    # -------- internals ----------
    def _connect_model_signals(self):
        if not self._cd:
            return
        model = self._cd.message_model()
        # Refresh meta when messages list changes
        model.rowsInserted.connect(lambda *_: self._refresh_meta())
        model.rowsRemoved.connect(lambda *_: self._refresh_meta())
        model.modelReset.connect(lambda *_: self._refresh_meta())

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
            # TODO: fill with real attachment export later
            payload["attachments"] = []  # stub

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            QMessageBox.critical(self, "Export failed", f"Could not write file:\n{e}")
            return

        QMessageBox.information(self, "Export complete", f"Chat exported to:\n{path}")
