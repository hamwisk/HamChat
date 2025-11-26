# hamchat/ui/widgets/chat_display.py
from __future__ import annotations

import time

from pathlib import Path
from dataclasses import dataclass
from typing import List, Any, Optional
from PyQt6.QtCore import pyqtSlot, Qt, QUrl, pyqtSignal, QAbstractListModel, QModelIndex, QVariant, QTimer
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLineEdit, QPushButton
from PyQt6.QtQuickWidgets import QQuickWidget
from PyQt6.QtQml import QQmlContext

from .prompt_input import PromptInput


# --- tiny model for attachments ---
class _AttachModel(QAbstractListModel):
    PATH_ROLE = Qt.ItemDataRole.UserRole + 50
    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[str] = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(self, idx, role=Qt.ItemDataRole.DisplayRole):
        if not idx.isValid(): return None
        if role == self.PATH_ROLE: return self._items[idx.row()]
        return None

    def roleNames(self):
        return { self.PATH_ROLE: b"path" }

    def contains(self, path: str) -> bool:
        return path in self._items

    def append_path(self, path: str):
        self.beginInsertRows(QModelIndex(), len(self._items), len(self._items))
        self._items.append(path)
        self.endInsertRows()

    def remove_at(self, row: int) -> str | None:
        if 0 <= row < len(self._items):
            self.beginRemoveRows(QModelIndex(), row, row)
            p = self._items.pop(row)
            self.endRemoveRows()
            return p
        return None

    def clear(self):
        if not self._items: return
        self.beginResetModel(); self._items.clear(); self.endResetModel()

    def snapshot(self) -> list[str]:
        return list(self._items)


# --- Minimal message model ---------------------------------------------------
@dataclass
class Msg:
    role: str   # "user" | "assistant" | "system"
    text: str
    thumbs: list[str] | None = None  # NEW (list of file paths)


class MessageListModel(QAbstractListModel):
    ROLE_ROLE   = Qt.ItemDataRole.UserRole + 1
    TEXT_ROLE   = Qt.ItemDataRole.UserRole + 2
    THUMBS_ROLE = Qt.ItemDataRole.UserRole + 3

    def __init__(self, messages: Optional[List[Msg]] = None, parent=None):
        super().__init__(parent)
        self._items: List[Msg] = messages or []

    # Qt model plumbing
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid(): return None
        m = self._items[index.row()]
        if role == self.ROLE_ROLE: return m.role
        if role == self.TEXT_ROLE: return m.text
        if role == self.THUMBS_ROLE: return m.thumbs or []  # NEW
        return None

    def roleNames(self):
        return {
            self.ROLE_ROLE: b"role",
            self.TEXT_ROLE: b"text",
            self.THUMBS_ROLE: b"thumbs",
        }

    def set_thumbs(self, row: int, paths: list[str]):
        if 0 <= row < len(self._items):
            self._items[row].thumbs = paths or []
            ix = self.index(row)
            self.dataChanged.emit(ix, ix, [self.THUMBS_ROLE])

    # convenience
    def append(self, msg: Msg):
        self.beginInsertRows(QModelIndex(), len(self._items), len(self._items))
        self._items.append(msg)
        self.endInsertRows()

    def append_and_index(self, msg: Msg) -> int:
        row = len(self._items)
        self.beginInsertRows(QModelIndex(), row, row)
        self._items.append(msg)
        self.endInsertRows()
        return row

    def set_text(self, row: int, new_text: str):
        if 0 <= row < len(self._items):
            self._items[row].text = new_text
            ix = self.index(row)
            self.dataChanged.emit(ix, ix, [self.TEXT_ROLE])

    def append_chunk(self, row: int, chunk: str):
        if 0 <= row < len(self._items):
            self._items[row].text += chunk
            ix = self.index(row)
            self.dataChanged.emit(ix, ix, [self.TEXT_ROLE])

    def clear(self):
        if not self._items:
            return
        self.beginResetModel()
        self._items.clear()
        self.endResetModel()

    # small helper so ChatDisplay can peek
    def get_text(self, row: int) -> str:
        if 0 <= row < len(self._items):
            return self._items[row].text
        return ""

    def to_list(self):
        return [{"role": m.role, "text": m.text, "thumbs": m.thumbs or []} for m in self._items]

    def __len__(self):
        return len(self._items)


# --- ChatDisplay widget ------------------------------------------------------
class ChatDisplay(QWidget):
    sig_send_text = pyqtSignal(str)
    sig_send_payload = pyqtSignal(str, list)
    sig_stop_requested = pyqtSignal()
    sig_file_dropped = pyqtSignal(str)
    sig_file_detected = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.PLACEHOLDER = "Thinking\u2026"
        self._qml_tokens = {}
        self._model = MessageListModel([])
        self._streaming = False
        self._last_action = 0.0
        self._attachments = _AttachModel(self)

        root = QVBoxLayout(self)
        root.setContentsMargins(0,0,0,0)
        root.setSpacing(0)

        # QML message view
        self.qml = QQuickWidget(self)
        self.qml.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        root.addWidget(self.qml, 1)

        # Input bar
        bar = QFrame(self)
        bl = QHBoxLayout(bar); bl.setContentsMargins(8, 8, 8, 8); bl.setSpacing(8)
        self.input = PromptInput(bar, min_h=28, max_h=120)
        self.send = QPushButton("Send", bar)
        self.send.setProperty("accent", True)
        self.send.setObjectName("SendButton")

        self.send.clicked.connect(self._on_send_clicked)
        self.input.submit.connect(self._on_send_clicked)
        self.input.fileDropped.connect(self.sig_file_dropped)
        self.input.fileDetected.connect(self.sig_file_detected)
        self.input.fileDetected.connect(self._on_file_detected)

        bl.addWidget(self.input, 1)
        bl.addWidget(self.send, 0)
        root.addWidget(bar, 0)

        # Load initial QML (after we have a context)
        self._load_qml()

    # --- public API for controller ---
    def set_streaming(self, on: bool) -> None:
        self._streaming = bool(on)
        self.input.setReadOnly(self._streaming)
        self.send.setText("Stop" if self._streaming else "Send")
        self.send.setProperty("accent", not self._streaming)  # subtle visual cue
        self.send.style().unpolish(self.send); self.send.style().polish(self.send)

    # called by MainWindow after theme applied
    def set_qml_tokens(self, tokens: dict) -> None:
        self._qml_tokens = tokens or {}
        self._reload_context()

    def append_message(self, role: str, text: str) -> None:
        self._call_qml("ensureAtEnd")
        self._model.append(Msg(role, text))

    # ------- internals -------
    def _on_send_clicked(self):
        now = time.monotonic()
        if (now - self._last_action) < 0.25:
            return
        self._last_action = now
        if self._streaming:
            self.sig_stop_requested.emit()
            return
        self._submit_text(self.input.toPlainText().strip())

    def _submit_text(self, text: str):
        if not text or self._streaming:
            return

        attachments = self._attachments.snapshot()  # snapshot first
        self.append_message("user", text)

        # EMIT ONLY ONE of these:
        if attachments:
            self.sig_send_payload.emit(text, attachments)
        else:
            self.sig_send_text.emit(text)

        self.input.clear()
        QTimer.singleShot(0, self.clear_attachments)

    def _call_qml(self, method: str):
        root = self.qml.rootObject()
        if root and hasattr(root, "children"):  # cheap existence check
            try:
                getattr(root.children()[0], method)()  # children()[0] is ListView
            except Exception:
                pass

    def begin_assistant_stream(self) -> int:
        # Create with placeholder so QML shows the spinner immediately
        row = self._model.append_and_index(Msg("assistant", self.PLACEHOLDER))
        self._call_qml("forceStickAndEnd")
        self._call_qml("ensureAtEnd")
        return row

    def stream_chunk(self, row: int, delta: str):
        # On the very first token, replace the placeholder (this hides the spinner)
        if self._model.get_text(row) == self.PLACEHOLDER:
            self._model.set_text(row, delta)
        else:
            self._model.append_chunk(row, delta)
        self._call_qml("ensureAtEnd")

    def end_assistant_stream(self, row: int):
        # If we ended without receiving any tokens, clear the placeholder so no spinner remains
        if self._model.get_text(row) == self.PLACEHOLDER:
            self._model.set_text(row, "")

    def _root_ctx(self) -> QQmlContext:
        return self.qml.rootContext()

    # QML bridge: remove a chip by index  NEW
    @pyqtSlot(int)
    def qmlRemoveAttachmentAt(self, index: int):
        self._attachments.remove_at(index)
        self._call_qml("ensureAtEnd")

    # (optional) open preview later
    @pyqtSlot(int)
    def qmlOpenAttachmentAt(self, index: int):
        pass

    # Direct, internal handler for detected files  NEW
    @pyqtSlot(str, str)
    def _on_file_detected(self, path: str, kind: str):
        if kind != "image":
            return
        norm = path[7:] if path.lower().startswith("file://") else path
        if not self._attachments.contains(norm):
            self._attachments.append_path(norm)
            self._call_qml("ensureAtEnd")

    # expose model + bridge to QML  (update your existing context reload)
    def _reload_context(self):
        ctx = self._root_ctx()
        ctx.setContextProperty("messageModel", self._model)
        ctx.setContextProperty("Theme", self._qml_tokens)
        ctx.setContextProperty("attachmentsModel", self._attachments)  # NEW
        ctx.setContextProperty("ChatBridge", self)                      # NEW
        self.qml.engine().clearComponentCache()
        self._set_qml_source()

    # convenience you can call from controller later
    def get_pending_attachments(self) -> list[str]:
        return self._attachments.snapshot()

    def clear_attachments(self):
        self._attachments.clear()

    def _load_qml(self):
        # initial context
        self._reload_context()

    def _set_qml_source(self):
        qml_dir = Path(__file__).resolve().parent / "qml"
        qml_dir.mkdir(parents=True, exist_ok=True)   # ensure path exists
        self.qml.setSource(QUrl.fromLocalFile(str(qml_dir / "ChatView.qml")))

    def clear_messages(self):
        # make sure we're not in "Stop" state and no spinner lingers
        self.set_streaming(False)
        self._model.clear()
        self._call_qml("ensureAtEnd")  # harmless even when empty

    # --- ChatDisplay: public helpers at class level
    def message_model(self):
        return self._model

    def message_count(self) -> int:
        return self._model.rowCount()

    def export_messages(self) -> list[dict]:
        return self._model.to_list()

    def draw_thumbs(self, paths: list[str]):
        def norm(p: str) -> str:
            return p if p.lower().startswith("file://") else "file://" + p
        thumb_urls = [norm(p) for p in paths if p]
        if not thumb_urls:
            return
        # Append a dedicated image-only bubble for the user with all thumbs
        self._model.append(Msg("user", "", thumb_urls))
        self._call_qml("ensureAtEnd")
