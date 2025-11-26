# hamchat/ui/widgets/top_panel.py
from __future__ import annotations
from PyQt6.QtCore import Qt, QPropertyAnimation, pyqtSignal, QEasingCurve
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QFrame

class TopPanel(QFrame):
    sig_closed = pyqtSignal()

    def __init__(self, parent=None, expanded_height: int = 240):
        super().__init__(parent)
        self._expanded = False
        self._expanded_height = expanded_height
        self._anim = QPropertyAnimation(self, b"maximumHeight", self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.setFrameShape(QFrame.Shape.NoFrame); self.setMaximumHeight(0)
        self.setStyleSheet("background:#3c4048;")
        self._host = QFrame(self)
        lay = QVBoxLayout(self); lay.setContentsMargins(0,0,0,0); lay.addWidget(self._host)
        self._host_lay = QVBoxLayout(self._host); self._host_lay.setContentsMargins(12,12,12,12); self._host_lay.setSpacing(8)

    def open_with(self, w: QWidget):
        self.clear(); self._host_lay.addWidget(w); self._set_expanded(True)

    def close_panel(self):
        self._set_expanded(False); self.clear(); self.sig_closed.emit()

    def clear(self):
        while self._host_lay.count():
            it = self._host_lay.takeAt(0); w = it.widget()
            if w: w.setParent(None); w.deleteLater()

    def _set_expanded(self, on: bool):
        if on == self._expanded: return
        self._expanded = on; target = self._expanded_height if on else 0
        self._anim.stop(); self._anim.setStartValue(self.maximumHeight()); self._anim.setEndValue(target); self._anim.start()
