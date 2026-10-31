import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from types import SimpleNamespace
import threading

import pytest
from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication, QMenuBar

from hamchat.core.session import SessionManager
from hamchat.core.settings import Settings
from hamchat.infra.llm.ollama_client import OllamaClient, RuntimeContext, UnloadResult
from hamchat.ui.model_lifecycle import ModelLifecycle, LifecycleWorker, _JOBS
from hamchat.ui.chat_controller import ChatController
from hamchat.ui.widgets.model_manager import ModelManager


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def drain_jobs():
    loop = QEventLoop()
    timer = QTimer()
    timer.timeout.connect(lambda: loop.quit() if not _JOBS else None)
    timer.start(5)
    QTimer.singleShot(2000, loop.quit)
    loop.exec()
    assert not _JOBS


def make_lifecycle(tmp_path):
    session = SessionManager(Settings(tmp_path / "app.json"), "solo", None)
    session.set_model_id("selected")
    session.set_model_context_allocation("selected", "high")
    client = OllamaClient(base_url="http://selected-backend")
    controller = SimpleNamespace(_model_client=client, _model_name="selected", _history=["existing"], broker=SimpleNamespace(active_ticket=lambda: -1))
    lifecycle = ModelLifecycle(session, controller)
    messages = []
    lifecycle.status.connect(messages.append)
    return session, client, controller, lifecycle, messages


def test_warm_selected_context_off_ui_thread_and_no_history(app, tmp_path, monkeypatch):
    session, client, controller, lifecycle, messages = make_lifecycle(tmp_path)
    calls = []
    main_thread = threading.get_ident()
    def prepare(**kwargs):
        calls.append((kwargs, threading.get_ident()))
        return RuntimeContext(16384, "runtime")
    monkeypatch.setattr(client, "prepare_runtime_context", prepare)
    monkeypatch.setattr(client, "unload_all_models", lambda: pytest.fail("warm must not unload"))
    lifecycle.start("warm")
    assert session.model_lifecycle_busy
    lifecycle.start("warm")
    assert "wait" in messages[-1]
    drain_jobs()
    assert calls == [({"model": "selected", "options": {"num_ctx": 16384}, "force": True}, calls[0][1])]
    assert calls[0][1] != main_thread
    assert controller._history == ["existing"]
    assert messages[-1] == "Loaded and warmed selected."
    assert not session.model_lifecycle_busy


def test_busy_generation_backend_and_failure_recovery(app, tmp_path, monkeypatch):
    session, client, ctrl, lifecycle, messages = make_lifecycle(tmp_path)
    ctrl.broker.active_ticket = lambda: 4
    lifecycle.start("unload")
    assert "wait" in messages[-1] and not _JOBS
    ctrl.broker.active_ticket = lambda: -1
    ctrl._model_client = object()
    lifecycle.start("warm")
    assert "unsupported" in messages[-1] and not _JOBS
    ctrl._model_client = client
    session.is_ollama_model = lambda _model: False
    lifecycle.start("unload")
    assert "unsupported" in messages[-1]
    session.is_ollama_model = lambda _model: True
    monkeypatch.setattr(client, "prepare_runtime_context", lambda **_: RuntimeContext(4096, "fallback"))
    lifecycle.start("warm")
    drain_jobs()
    assert "failed" in messages[-1] and not session.model_lifecycle_busy
    def fail(): raise ConnectionError("offline")
    monkeypatch.setattr(client, "unload_all_models", fail)
    lifecycle.start("unload")
    drain_jobs()
    assert "offline" in messages[-1] and not session.model_lifecycle_busy
    calls = []
    monkeypatch.setattr(client, "unload_all_models", lambda: calls.append(client.base_url) or UnloadResult(2, ("chat", "embed"), (), (), ()))
    lifecycle.start("unload")
    drain_jobs()
    assert calls == ["http://selected-backend"]
    assert messages[-1] == "Unloaded 2 model(s)."
    assert ctrl._history == ["existing"]


def test_menu_order_routing_and_busy_disabling(app):
    from test_about_menu import _menus
    bar = QMenuBar()
    menus = _menus(bar, lambda: None)
    calls = []
    menus._unload_all_models = lambda: calls.append("unload")
    menus._warm_selected_model = lambda: calls.append("warm")
    menus.build()
    model_menu = next(action.menu() for action in bar.actions() if action.text() == "&Model")
    first, second = model_menu.actions()[:2]
    assert [first.text(), second.text()] == ["Unload all models", "Load and warm selected model"]
    first.trigger(); second.trigger()
    assert calls == ["unload", "warm"]
    menus._model_actions_available = lambda: False
    model_menu.aboutToShow.emit()
    assert not first.isEnabled() and not second.isEnabled()
    menus._model_actions_available = lambda: True
    model_menu.aboutToShow.emit()
    assert first.isEnabled() and second.isEnabled()


def test_manager_and_generation_share_lifecycle_guard(app, tmp_path):
    session, client, ctrl, lifecycle, messages = make_lifecycle(tmp_path)
    manager = ModelManager(session)
    session.set_model_lifecycle_busy(True)
    manager._on_unload_all_clicked()
    assert "Wait" in manager.unload_status.text()
    # Controller exits before persistence/history changes for either request path.
    state = SimpleNamespace(_session=session)
    state._lifecycle_blocked = lambda: ChatController._lifecycle_blocked(state)
    ChatController._on_user_text(state, "text")
    ChatController.send_user_with_media(state, "image", [])
    ChatController.resend_message(state, 0)
    session.set_model_lifecycle_busy(False)
    assert not state._lifecycle_blocked()
    manager.close()


def test_forced_preparation_reloads_cached_model_without_unload(monkeypatch):
    from test_ollama_context_allocation import Response
    calls = []
    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.post", lambda url, **kwargs: calls.append((url, kwargs["json"])) or Response({}))
    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.get", lambda *_a, **_k: Response({"models": [{"name": "selected", "context_length": 8192}]}))
    client = OllamaClient()
    for force in (False, False, True):
        client.prepare_runtime_context(model="selected", options={"num_ctx": 8192}, force=force)
    assert len(calls) == 2
    assert all(payload == {"model": "selected", "prompt": "", "stream": False, "options": {"num_ctx": 8192}} for _, payload in calls)


def test_menu_unload_reaches_generation_and_embedding_endpoints(app, tmp_path, monkeypatch):
    from test_ollama_unload import _Response, _ps
    session, client, ctrl, lifecycle, messages = make_lifecycle(tmp_path)
    running = iter([_ps(["selected", "embedding"]), _ps([])])
    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.get", lambda *_a, **_k: next(running))
    posts = []
    def post(url, **kwargs):
        payload = kwargs["json"]
        posts.append((url, payload))
        if url.endswith("/api/show"):
            return _Response({"capabilities": ["embedding" if payload["name"] == "embedding" else "completion"]})
        return _Response()
    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.post", post)
    lifecycle.start("unload")
    drain_jobs()
    unloads = [(url, payload) for url, payload in posts if "keep_alive" in payload]
    assert [(url, payload["model"]) for url, payload in unloads] == [
        ("http://selected-backend/api/generate", "selected"),
        ("http://selected-backend/api/embed", "embedding")]
    assert all(payload["keep_alive"] == 0 for _, payload in unloads)
    assert messages[-1] == "Unloaded 2 model(s)."
    assert not session.model_lifecycle_busy


def test_model_manager_unload_releases_shared_busy_state_on_failure(app, tmp_path, monkeypatch):
    from hamchat.ui.widgets.model_manager import _ACTIVE_UNLOAD_JOBS
    session, client, ctrl, lifecycle, messages = make_lifecycle(tmp_path)
    manager = ModelManager(session, ollama_client=client)
    monkeypatch.setattr(client, "unload_all_models", lambda **_: UnloadResult(0, (), (), (), None, "offline"))
    manager._on_unload_all_clicked()
    assert session.model_lifecycle_busy
    lifecycle.start("warm")
    assert "wait" in messages[-1]
    loop = QEventLoop()
    timer = QTimer()
    timer.timeout.connect(lambda: loop.quit() if not _ACTIVE_UNLOAD_JOBS else None)
    timer.start(5)
    QTimer.singleShot(2000, loop.quit)
    loop.exec()
    assert not _ACTIVE_UNLOAD_JOBS
    assert not session.model_lifecycle_busy
    assert "offline" in manager.unload_status.text()
    assert manager.btn_unload_all.isEnabled()
    manager.close()
