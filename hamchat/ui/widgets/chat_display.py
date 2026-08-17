# hamchat/ui/widgets/chat_display.py
from __future__ import annotations

import html
import time

from pathlib import Path
from dataclasses import dataclass
from typing import Callable, List, Any, Optional
from PyQt6.QtCore import pyqtSlot, Qt, QUrl, pyqtSignal, QAbstractListModel, QModelIndex, QVariant, QTimer, QSize
from PyQt6.QtGui import QGuiApplication, QIcon
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLineEdit, QPushButton, QToolButton, QFileDialog
from PyQt6.QtQuickWidgets import QQuickWidget
from PyQt6.QtQml import QQmlContext

from .prompt_input import PromptInput
from hamchat.media_helper import normalize_image_file, ImageValidationError


IMAGE_FILE_FILTER = (
    "Supported images (*.jpg *.jpeg *.png *.webp *.gif *.bmp *.tif *.tiff);;All files (*)"
)


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
    render_markdown: bool = True


def _raw_html_end(text: str, start: int) -> int | None:
    """Return the exclusive end of an HTML-like tag starting at ``start``."""
    if text.startswith("<!--", start):
        end = text.find("-->", start + 4)
        return len(text) if end < 0 else end + 3

    marker = start + 1
    if marker < len(text) and text[marker] in "!?":
        marker += 1
    elif marker < len(text) and text[marker] == "/":
        marker += 1
    if marker >= len(text) or not text[marker].isalpha():
        return None

    quote = ""
    for pos in range(marker + 1, len(text)):
        char = text[pos]
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == ">":
            return pos + 1
    return len(text)


def _safe_inline_markdown(text: str) -> str:
    """Keep Markdown syntax while neutralizing images and raw HTML outside code."""
    output: list[str] = []
    pos = 0
    while pos < len(text):
        char = text[pos]
        if char == "\\" and pos + 1 < len(text):
            output.append(text[pos:pos + 2])
            pos += 2
            continue
        if char == "`":
            run_end = pos
            while run_end < len(text) and text[run_end] == "`":
                run_end += 1
            delimiter = text[pos:run_end]
            close = text.find(delimiter, run_end)
            if close >= 0:
                output.append(text[pos:close + len(delimiter)])
                pos = close + len(delimiter)
                continue
        if char == "!" and pos + 1 < len(text) and text[pos + 1] == "[":
            # Escaping only the image marker keeps its source visible but makes
            # both inline and reference-style images ordinary Markdown text.
            output.append("\\!")
            pos += 1
            continue
        if char == "<":
            tag_end = _raw_html_end(text, pos)
            if tag_end is not None:
                output.append(html.escape(text[pos:tag_end]))
                pos = tag_end
                continue
        output.append(char)
        pos += 1
    return "".join(output)


def _fence_opening(line: str) -> tuple[str, int] | None:
    """Return a Markdown fence marker when ``line`` opens a code block."""
    indent = len(line) - len(line.lstrip(" "))
    if indent > 3 or indent >= len(line):
        return None
    marker = line[indent]
    if marker not in "`~":
        return None
    end = indent
    while end < len(line) and line[end] == marker:
        end += 1
    length = end - indent
    if length < 3:
        return None
    if marker == "`" and "`" in line[end:].rstrip("\r\n"):
        return None
    return marker, length


def _is_fence_closing(line: str, marker: str, minimum_length: int) -> bool:
    indent = len(line) - len(line.lstrip(" "))
    if indent > 3 or indent >= len(line) or line[indent] != marker:
        return False
    end = indent
    while end < len(line) and line[end] == marker:
        end += 1
    return (end - indent) >= minimum_length and not line[end:].strip(" \t\r\n")


def _without_fence_terminator(code: str) -> str:
    """Drop only the line ending that structurally precedes a closing fence."""
    if code.endswith("\r\n"):
        return code[:-2]
    if code.endswith(("\n", "\r")):
        return code[:-1]
    return code


def message_display_blocks(text: str, *, render_markdown: bool) -> list[dict[str, str]]:
    """Return safe, display-only blocks while leaving canonical message text intact."""
    if not render_markdown:
        return [{"kind": "plain", "text": text}]

    blocks: list[dict[str, str]] = []
    normal_lines: list[str] = []
    fenced_lines: list[str] | None = None
    fence_marker = ""
    fence_length = 0

    def append_markdown(lines: list[str]) -> None:
        markdown = _safe_inline_markdown("".join(lines))
        if markdown.strip():
            blocks.append({"kind": "markdown", "text": markdown})

    for line in text.splitlines(keepends=True):
        if fenced_lines is not None:
            if _is_fence_closing(line, fence_marker, fence_length):
                blocks.append({"kind": "code", "text": _without_fence_terminator("".join(fenced_lines))})
                fenced_lines = None
                fence_marker = ""
                fence_length = 0
            else:
                fenced_lines.append(line)
            continue

        opening = _fence_opening(line)
        if opening is not None:
            if normal_lines:
                append_markdown(normal_lines)
                normal_lines.clear()
            fence_marker, fence_length = opening
            fenced_lines = []
        else:
            normal_lines.append(line)
    if fenced_lines is not None:
        blocks.append({"kind": "code", "text": "".join(fenced_lines)})
    if normal_lines:
        append_markdown(normal_lines)
    return blocks


class MessageListModel(QAbstractListModel):
    ROLE_ROLE   = Qt.ItemDataRole.UserRole + 1
    TEXT_ROLE   = Qt.ItemDataRole.UserRole + 2
    THUMBS_ROLE = Qt.ItemDataRole.UserRole + 3
    DISPLAY_BLOCKS_ROLE = Qt.ItemDataRole.UserRole + 4
    RENDER_MARKDOWN_ROLE = Qt.ItemDataRole.UserRole + 5

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
        if role == self.DISPLAY_BLOCKS_ROLE:
            return message_display_blocks(m.text, render_markdown=m.render_markdown)
        if role == self.RENDER_MARKDOWN_ROLE: return m.render_markdown
        return None

    def roleNames(self):
        return {
            self.ROLE_ROLE: b"role",
            self.TEXT_ROLE: b"text",
            self.THUMBS_ROLE: b"thumbs",
            self.DISPLAY_BLOCKS_ROLE: b"displayBlocks",
            self.RENDER_MARKDOWN_ROLE: b"renderMarkdown",
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

    def insert(self, row: int, msg: Msg) -> int:
        row = max(0, min(row, len(self._items)))
        self.beginInsertRows(QModelIndex(), row, row)
        self._items.insert(row, msg)
        self.endInsertRows()
        return row

    def set_text(self, row: int, new_text: str):
        if 0 <= row < len(self._items):
            self._items[row].text = new_text
            ix = self.index(row)
            self.dataChanged.emit(ix, ix, [self.TEXT_ROLE, self.DISPLAY_BLOCKS_ROLE])

    def append_chunk(self, row: int, chunk: str):
        if 0 <= row < len(self._items):
            self._items[row].text += chunk
            ix = self.index(row)
            self.dataChanged.emit(ix, ix, [self.TEXT_ROLE, self.DISPLAY_BLOCKS_ROLE])

    def set_render_markdown(self, row: int, enabled: bool) -> None:
        if 0 <= row < len(self._items):
            message = self._items[row]
            if message.render_markdown == bool(enabled):
                return
            message.render_markdown = bool(enabled)
            ix = self.index(row)
            self.dataChanged.emit(ix, ix, [self.DISPLAY_BLOCKS_ROLE, self.RENDER_MARKDOWN_ROLE])

    def clear(self):
        if not self._items:
            return
        self.beginResetModel()
        self._items.clear()
        self.endResetModel()

    def truncate_from(self, start_row: int) -> None:
        """
        Remove all messages from start_row to the end of the model.
        """
        if not (0 <= start_row < len(self._items)):
            return
        end_row = len(self._items) - 1
        self.beginRemoveRows(QModelIndex(), start_row, end_row)
        del self._items[start_row:]
        self.endRemoveRows()

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
    attachmentRejected = pyqtSignal(str)
    bubbleAction = pyqtSignal(str, int, str, str)

    def __init__(self, parent=None, *, local_command_handler: Optional[Callable[[str], bool]] = None):
        super().__init__(parent)
        self._local_command_handler = local_command_handler
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
        self.attach = QToolButton(bar)
        self.attach.setObjectName("AttachButton")
        self.attach.setAccessibleName("Attach files")
        self.attach.setToolTip("Attach files")
        self.attach.setStatusTip("Attach files")
        self.attach.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.attach.setMinimumSize(44, 44)
        self.attach.setIconSize(QSize(16, 16))
        attachment_icon = QIcon.fromTheme("mail-attachment-symbolic")
        if attachment_icon.isNull():
            attachment_icon = QIcon.fromTheme("mail-attachment")
        if attachment_icon.isNull():
            self.attach.setText("📎")
        else:
            self.attach.setIcon(attachment_icon)
        self.input = PromptInput(bar, min_h=28, max_h=120)
        self.send = QPushButton("Send", bar)
        self.send.setProperty("accent", True)
        self.send.setObjectName("SendButton")

        self.attach.clicked.connect(self._choose_attachments)
        self.send.clicked.connect(self._on_send_clicked)
        self.input.submit.connect(self._on_send_clicked)
        self.input.fileDropped.connect(self.sig_file_dropped)
        self.input.fileDetected.connect(self.sig_file_detected)
        self.input.fileDetected.connect(self._on_file_detected)

        bl.addWidget(self.attach, 0)
        bl.addWidget(self.input, 1)
        bl.addWidget(self.send, 0)
        root.addWidget(bar, 0)

        # Load initial QML (after we have a context)
        self._load_qml()

    # --- public API for controller ---
    def set_streaming(self, on: bool) -> None:
        self._streaming = bool(on)
        self.input.setReadOnly(self._streaming)
        self.input.setAcceptDrops(not self._streaming)
        self.attach.setEnabled(not self._streaming)
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
        # Don't allow submitting while streaming a response
        if self._streaming:
            return

        if self._local_command_handler:
            try:
                if self._local_command_handler(text):
                    self.input.clear()
                    QTimer.singleShot(0, self.clear_attachments)
                    return
            except Exception:
                pass

        # Take a snapshot of attachments *before* we decide to bail
        attachments = self._attachments.snapshot()

        # Bail out only if there is no text AND no attachments
        if (not text) and (not attachments):
            return

        # Only append a text bubble if there actually is text
        if text:
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
        row = self._model.append_and_index(Msg("assistant", self.PLACEHOLDER, render_markdown=False))
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

    def end_assistant_stream(self, row: int, *, successful: bool):
        # If we ended without receiving any tokens, clear the placeholder so no spinner remains
        if self._model.get_text(row) == self.PLACEHOLDER:
            self._model.set_text(row, "")
        elif successful:
            self._model.set_render_markdown(row, True)
            self._call_qml("ensureAtEnd")

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

    def _choose_attachments(self) -> None:
        try:
            paths, _ = QFileDialog.getOpenFileNames(
                self,
                "Attach files",
                "",
                IMAGE_FILE_FILTER,
            )
            for path in paths:
                self._stage_attachment(path, "image")
        finally:
            self.input.setFocus(Qt.FocusReason.OtherFocusReason)

    def _stage_attachment(self, path: str, kind: str) -> None:
        """Validate and add one attachment path to the pending model."""
        norm = path[7:] if path.lower().startswith("file://") else path
        try:
            normalize_image_file(norm)
        except ImageValidationError:
            if kind == "image":
                self.attachmentRejected.emit(
                    "HamChat couldn’t read this image. The file may be corrupt or use an unsupported format."
                )
            return
        if not self._attachments.contains(norm):
            self._attachments.append_path(norm)
            self._call_qml("ensureAtEnd")

    # Direct, internal handler for detected files  NEW
    @pyqtSlot(str, str)
    def _on_file_detected(self, path: str, kind: str):
        self._stage_attachment(path, kind)

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
        self._wire_qml_signals()

    def _wire_qml_signals(self):
        root_obj = self.qml.rootObject()
        if not root_obj:
            return
        try:
            root_obj.bubbleActionRequested.disconnect(self._on_bubble_action)
        except Exception:
            pass
        try:
            root_obj.bubbleActionRequested.connect(self._on_bubble_action)
        except Exception:
            pass

    def _on_bubble_action(self, action: str, index: int, role: str, text: str):
        if action == "copy":
            QGuiApplication.clipboard().setText(text or "")
            return
        self.bubbleAction.emit(action, index, role, text)

    def clear_messages(self):
        # make sure we're not in "Stop" state and no spinner lingers
        self.set_streaming(False)
        self._model.clear()
        self._call_qml("ensureAtEnd")  # harmless even when empty

    def truncate_messages_from(self, index: int) -> None:
        """
        Remove all visible bubbles from the given index to the end.
        Used by resend/regenerate when truncating a conversation tail.
        """
        self._model.truncate_from(index)
        self._call_qml("ensureAtEnd")

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

    def insert_thumbs_after(self, row: int, paths: list[str]):
        def norm(p: str) -> str:
            return p if p.lower().startswith("file://") else "file://" + p
        thumb_urls = [norm(p) for p in paths if p]
        if not thumb_urls:
            return
        insert_at = max(0, row + 1)
        self._model.insert(insert_at, Msg("user", "", thumb_urls))
        self._call_qml("ensureAtEnd")

    # ---- helpers for bubble actions ----------------------------------------
    def _normalize_paths(self, paths: list[str]) -> list[str]:
        normed = []
        for p in paths:
            if not p:
                continue
            normed.append(p[7:] if p.lower().startswith("file://") else p)
        return normed

    def get_message_payload(self, index: int) -> Optional[dict]:
        """
        Return a logical message payload for the given bubble index, including any
        attached thumbs that belong to the same user message.
        """
        items = getattr(self._model, "_items", [])
        if not (0 <= index < len(items)):
            return None

        msg = items[index]
        base_idx = index
        text = msg.text or ""
        attachments: list[str] = []

        if msg.role == "user":
            # --- 1) Decide which logical user message this bubble belongs to ---
            if not text:
                # Only walk back through *contiguous user bubbles*;
                # stop as soon as we hit a non-user role (assistant/system/etc).
                for i in range(index - 1, -1, -1):
                    prev = items[i]
                    if prev.role != "user":
                        break
                    if prev.text:
                        base_idx = i
                        text = prev.text
                        break

            # --- 2) Collect thumbs from the clicked bubble (if any) ---
            if getattr(msg, "thumbs", None):
                attachments.extend(msg.thumbs)

            # --- 3) If we're on the base text bubble, also grab thumbs
            #         from the immediate following user bubble (if it's
            #         an image-only bubble).
            if index == base_idx and 0 <= base_idx + 1 < len(items):
                nxt = items[base_idx + 1]
                if nxt.role == "user" and not nxt.text and getattr(nxt, "thumbs", None):
                    attachments.extend(nxt.thumbs)

            return {
                "role": "user",
                "text": text,
                "attachments": self._normalize_paths(attachments),
                "base_index": base_idx,
            }

        # Non-user bubble: treat it as-is; attachments are always empty here.
        return {
            "role": msg.role,
            "text": text,
            "attachments": [],
            "base_index": base_idx,
        }

    def scroll_to_base_index(self, base_index: int) -> None:
        """
        Scroll the chat view to the given base index (UI row).
        """
        try:
            idx = int(base_index)
        except Exception:
            return
        if idx < 0 or idx >= self._model.rowCount():
            return

        root = self.qml.rootObject()
        if not root:
            return
        children = getattr(root, "children", lambda: [])()
        if not children:
            return
        view = children[0]
        try:
            view.stickToBottom = False
        except Exception:
            pass
        try:
            view.positionViewAtIndex(idx, 0)
        except Exception:
            try:
                view.positionViewAtIndex(idx)
            except Exception:
                pass

    def get_user_payload(self, index: int) -> Optional[dict]:
        payload = self.get_message_payload(index)
        if payload and payload.get("role") == "user":
            return payload
        for i in range(index - 1, -1, -1):
            candidate = self.get_message_payload(i)
            if candidate and candidate.get("role") == "user":
                return candidate
        return None

    def set_pending_attachments(self, paths: list[str]) -> None:
        """Populate the pending attachments model (used by edit & resend)."""
        self.clear_attachments()
        for p in self._normalize_paths(paths or []):
            if not self._attachments.contains(p):
                self._attachments.append_path(p)
        self._call_qml("ensureAtEnd")

    def add_pending_attachments_from_paths(self, paths: list[str]) -> None:
        """
        Append attachments to the pending list, deduping existing entries.
        """
        updated = False
        for p in self._normalize_paths(paths or []):
            if not self._attachments.contains(p):
                self._attachments.append_path(p)
                updated = True
        if updated:
            self._call_qml("ensureAtEnd")
