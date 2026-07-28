from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QFrame, QScrollArea, QVBoxLayout, QWidget


class TopPanel(QFrame):
    """Splitter-managed, scrollable host for Python top-panel tools."""
    sig_closed = pyqtSignal()
    sig_opened = pyqtSignal()

    def __init__(self, parent=None, expanded_height: int = 240):
        super().__init__(parent)
        self._expanded = False
        self._expanded_height = expanded_height
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet("background:#3c4048;")
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._host = QFrame(self._scroll)
        self._scroll.setWidget(self._host)
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0); lay.addWidget(self._scroll)
        self._host_lay = QVBoxLayout(self._host); self._host_lay.setContentsMargins(12, 12, 12, 12); self._host_lay.setSpacing(8)
        self.hide()

    def open_with(self, w: QWidget):
        was_expanded = self._expanded
        self.clear(); self._host_lay.addWidget(w); self._expanded = True; self.show()
        if not was_expanded:
            self.sig_opened.emit()

    def close_panel(self):
        if not self._expanded:
            return
        self._expanded = False
        self.clear(); self.hide(); self.sig_closed.emit()

    def clear(self):
        while self._host_lay.count():
            item = self._host_lay.takeAt(0); widget = item.widget()
            if widget:
                widget.setParent(None)
                widget.deleteLater()
