# hamchat/ui/widgets/prompt_input.py
from __future__ import annotations
from typing import Optional
import os, mimetypes
from PyQt6.QtCore import Qt, pyqtSignal, QMimeData
from PyQt6.QtGui import QTextOption, QKeyEvent, QTextCursor
from PyQt6.QtWidgets import QTextEdit, QFrame

# --- Optional spellcheck (won’t crash if missing) ----------------------------
try:
    from hamchat.core.spell_highlighter import SpellHighlighter, DictionaryFactory
except Exception:
    SpellHighlighter = None
    DictionaryFactory = None

def _guess_kind(path: str) -> str:
    mt, _ = mimetypes.guess_type(path)
    if not mt:
        return "file"
    top = mt.split("/", 1)[0]
    return {"image":"image","audio":"audio","video":"video","text":"text","application":"doc"}.get(top, "file")

class PromptInput(QTextEdit):
    """Multi-line message input with auto-height, Enter-to-send, drag & drop, and spellcheck hooks."""
    submit = pyqtSignal(str)              # emitted on Enter (without Shift)
    fileDetected = pyqtSignal(str, str)   # (path, kind)
    fileDropped = pyqtSignal(str)         # (path)

    def __init__(self, parent=None, min_h: int = 10, max_h: int = 120):
        super().__init__(parent)
        self.setObjectName("PromptInput")
        self._min_h, self._max_h = min_h, max_h

        # Text behavior
        self.setPlaceholderText("Type your message…")
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAcceptDrops(True)

        # Aesthetic – let QSS style the surface, keep widget clean
        self.setStyleSheet("QTextEdit#PromptInput { border: none; background: transparent; padding: 0; }")

        # Auto-height
        self.textChanged.connect(self._adjust_height)
        self._adjust_height()

        # Spellcheck (if available)
        if DictionaryFactory and SpellHighlighter:
            try:
                self._spell_dict = DictionaryFactory.get_dict()
                self._highlighter = SpellHighlighter(self.document())
            except Exception:
                self._spell_dict = None
                self._highlighter = None
        else:
            self._spell_dict = None
            self._highlighter = None

    # ---- sizing --------------------------------------------------------------
    def _adjust_height(self):
        doc_h = int(self.document().size().height()) + 8
        self.setFixedHeight(max(self._min_h, min(doc_h, self._max_h)))

    # ---- key handling --------------------------------------------------------
    def keyPressEvent(self, e: QKeyEvent):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (e.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            e.accept()
            text = self.toPlainText().strip()
            # Always emit; ChatDisplay decides whether this means Send or Stop
            self.submit.emit(text)
            if text:
                self.clear()
                self._adjust_height()
            return
        super().keyPressEvent(e)

    # ---- drag & drop ---------------------------------------------------------
    def canInsertFromMimeData(self, src: QMimeData) -> bool:
        return False if src.hasUrls() else super().canInsertFromMimeData(src)

    def insertFromMimeData(self, src: QMimeData) -> None:
        if src.hasUrls():
            return
        return super().insertFromMimeData(src)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e):
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile() or urls[0].toString()
            if path:
                self.fileDropped.emit(path)
                self.fileDetected.emit(path, _guess_kind(path))
            e.acceptProposedAction()
            super().dropEvent(e)     # keeps cursor/selection sane
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self.ensureCursorVisible()
            return
        super().dropEvent(e)

    # ---- context menu + spelling --------------------------------------------
    def contextMenuEvent(self, event):
        # If no spell dict, fall back to default menu
        if not self._spell_dict:
            return super().contextMenuEvent(event)

        cursor = self.cursorForPosition(event.pos())
        cursor.select(QTextCursor.SelectionType.WordUnderCursor)
        word = cursor.selectedText()
        if not word or self._spell_dict.check(word):
            return super().contextMenuEvent(event)

        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        suggestions = self._spell_dict.suggest(word)
        if suggestions:
            for s in suggestions[:5]:
                act = menu.addAction(s)
                act.triggered.connect(lambda checked=False, s=s: self._replace_word(cursor, s))
        else:
            menu.addAction("[No suggestions]")
        menu.addSeparator()
        std = self.createStandardContextMenu()
        for a in std.actions():
            menu.addAction(a)
        menu.exec(event.globalPos())

    def _replace_word(self, cursor: QTextCursor, new_word: str):
        cursor.beginEditBlock()
        cursor.removeSelectedText()
        cursor.insertText(new_word)
        cursor.endEditBlock()

    def set_spell_enabled(self, on: bool) -> None:
        if self._highlighter:
            self._highlighter.setEnabled(bool(on))

    def set_spell_locale(self, locale: str) -> bool:
        if not DictionaryFactory or not self._highlighter:
            return False
        ok = DictionaryFactory.set_locale(locale)
        if ok:
            self._highlighter.dict = DictionaryFactory.get_dict()
            self._highlighter.rehighlight()
        return ok
