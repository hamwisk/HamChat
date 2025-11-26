# hamchat/ui/widgets/test_form.py
from __future__ import annotations
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QPushButton

class TestForm(QWidget):
    sig_close = pyqtSignal()
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("<b>Test Form</b><br/>Loaded by side panel."))
        btn = QPushButton("Close"); btn.clicked.connect(self.sig_close.emit); lay.addWidget(btn)
        lay.addStretch(1)
