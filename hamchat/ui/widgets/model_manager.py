# hamchat/ui/widgets/model_manager.py
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


class ModelManagerForm(QWidget):
    """
    Minimal MVP model manager: just lists models and highlights the current one.
    The actual selection is handled by the menu; this is just an info panel.
    """
    sig_close = pyqtSignal()

    def __init__(self, models: list[tuple[str, str]], current_model: str, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)

        lay.addWidget(QLabel("<b>Models</b>"))

        if not models:
            lay.addWidget(QLabel("No models are configured yet."))
        else:
            html = "<ul>"
            for model_id, label in models:
                suffix = " — <b>current</b>" if model_id == current_model else ""
                html += f"<li>{label} <code>{model_id}</code>{suffix}</li>"
            html += "</ul>"
            lay.addWidget(QLabel(html))

        btn = QPushButton("Close")
        btn.clicked.connect(self.sig_close.emit)
        lay.addWidget(btn)
        lay.addStretch(1)

