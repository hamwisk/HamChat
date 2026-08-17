from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from hamchat.infra.llm.ollama_client import UnloadResult
from hamchat.ui.widgets.model_manager import ModelManager


class _Session:
    selected_model = "chat"

    def get_model_choices(self):
        return [("chat", "chat")]

    def get_model_backend(self, _model):
        return None

    def get_model_capabilities(self, _model):
        return {}

    def get_model_context(self, _model):
        return None

    def is_ollama_model(self, _model):
        return True

    def get_model_context_allocation(self, _model):
        return "auto"

    def set_model_context_allocation(self, _model, _mode):
        pass

    def get_model_metadata(self, _model):
        return {"ollama_capabilities": ["completion"]}


@pytest.fixture
def manager():
    app = QApplication.instance() or QApplication([])
    widget = ModelManager(_Session())
    widget.show()
    app.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    app.processEvents()


def test_unload_button_is_accessible_and_active_generation_is_refused(manager):
    manager._is_generation_active = lambda: True

    manager.btn_unload_all.click()

    assert manager.btn_unload_all.accessibleName() == "Unload all models"
    assert "RAM/VRAM" in manager.btn_unload_all.toolTip()
    assert manager.unload_status.text() == "Stop or wait for the current response before unloading models."


def test_busy_unload_is_deduplicated_and_result_keeps_selected_model(manager):
    manager._unload_busy = True
    manager._on_unload_all_clicked()
    assert manager._unload_busy is True

    manager._unload_busy = False
    manager._on_unload_finished(1, (UnloadResult(2, ("chat",), (), ("unknown",), ("unknown",)), ""))

    assert manager._session.selected_model == "chat"
    assert manager.unload_status.text() == "Unloaded 1 model(s); failed 0; skipped 1."
