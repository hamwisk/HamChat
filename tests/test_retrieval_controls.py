import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from types import SimpleNamespace

import numpy as np
import pytest
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from hamchat.core.session import SessionManager
from hamchat.core.settings import Settings
from hamchat.infra.llm.base import ModelClient, StreamEvent
from hamchat.memory_embeddings import MemoryEmbeddingService, validate_retrieval
from hamchat.ui.chat_controller import ChatController
from hamchat.ui.widgets.chat_panel import ChatPanel


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_defaults_changes_restart_and_user_isolation(tmp_path):
    path = tmp_path / "app.json"
    session = SessionManager(Settings(path), "solo", None)
    assert session.get_ham_mem_retrieval() == (5, .25)
    session.set_ham_mem_retrieval(9, -.15)
    session.load_user(1, "one", "user", {})
    assert session.get_ham_mem_retrieval() == (5, .25)
    session.set_ham_mem_retrieval(2, .8)
    session.load_user(2, "two", "user", {})
    assert session.get_ham_mem_retrieval() == (5, .25)
    restored = SessionManager(Settings(path), "solo", None)
    assert restored.get_ham_mem_retrieval() == (9, -.15)
    restored.load_user(1, "one", "user", {})
    assert restored.get_ham_mem_retrieval() == (2, .8)
    restored.settings.set("ham_mem_retrieval", {"1": {"top_k": True}})
    assert restored.get_ham_mem_retrieval() == (5, .25)


@pytest.mark.parametrize("count,threshold", [(0, .25), (101, .25), (1.5, .25), (True, .25), (5, float("nan")), (5, 1.1), (5, -1.1), (5, True)])
def test_invalid_retrieval_limits(count, threshold):
    with pytest.raises(ValueError):
        validate_retrieval(count, threshold)


def test_panel_disables_without_reset_and_restores(app):
    panel = ChatPanel()
    assert (panel._memory_maximum.value(), panel._memory_relevance.value()) == (5, .25)
    changes = []
    panel.hamMemRetrievalChanged.connect(lambda *values: changes.append(values))
    panel._memory_maximum.setValue(3)
    panel._memory_relevance.setValue(.7)
    assert changes[-1] == (3, .7)
    panel._use_ham_mem.setChecked(False)
    assert not panel._memory_maximum.isEnabled()
    assert not panel._memory_relevance.isEnabled()
    assert panel._memory_relevance.value() == .7
    panel.set_use_ham_mem(True)
    assert panel._memory_maximum.isEnabled()
    panel.set_ham_mem_retrieval(8, -.4)
    assert (panel._memory_maximum.value(), panel._memory_relevance.value()) == (8, -.4)
    panel.set_use_ham_mem(True, control_enabled=False)
    assert not panel._memory_maximum.isEnabled()
    panel.close()


class Provider:
    def embed_one(self, _query): return np.array([1., 0.])


def test_threshold_and_count_keep_full_content_and_global():
    snapshot = ([(1, "full " * 2000, .5, np.array([1., 0.])),
                 (2, "second", .5, np.array([.8, .6])),
                 (3, "weak", .5, np.array([0., 1.]))], ["global"])
    _, _, details = MemoryEmbeddingService.format_context("q", Provider(), snapshot, top_k=1, min_similarity=.5, return_details=True)
    assert details == {"semantic": ["full " * 2000], "managed": ["global"]}
    _, _, details = MemoryEmbeddingService.format_context("q", Provider(), snapshot, top_k=5, min_similarity=.9, return_details=True)
    assert len(details["semantic"]) == 1
    _, _, details = MemoryEmbeddingService.format_context("q", Provider(), snapshot, top_k=5, min_similarity=-1, return_details=True)
    assert len(details["semantic"]) == 3


class Chat(QObject):
    sig_send_text = pyqtSignal(str)
    sig_stop_requested = pyqtSignal()
    def begin_assistant_stream(self): return 0
    def set_streaming(self, _value): pass


class Client(ModelClient):
    def __init__(self): self.requests = []
    def stream_chat(self, **kwargs):
        self.requests.append(kwargs)
        yield StreamEvent(type="end")


@pytest.mark.parametrize("image", [False, True])
def test_settings_reach_selection_in_text_and_image(app, tmp_path, monkeypatch, image):
    session = SessionManager(Settings(tmp_path / "settings.json"), "solo", None)
    session.set_ham_mem_retrieval(2, .75)
    client, panel, chat = Client(), ChatPanel(), Chat()
    controller = ChatController(chat, client, model_name="chat", session=session, thinking_panel=panel)
    controller._memory_provider = Provider()
    snapshot = ([(1, "strong", .5, np.array([1., 0.])), (2, "weak", .5, np.array([.5, .866]))], ["global"])
    controller._memory_snapshot = lambda *_: snapshot
    controller._persist_user_with_attachments = lambda *_: None
    controller.system_injection_if_any = lambda: None
    captured = []
    original = MemoryEmbeddingService.format_context
    def selection(*args, **kwargs):
        captured.append((kwargs["top_k"], kwargs["min_similarity"]))
        return original(*args, **kwargs)
    monkeypatch.setattr(MemoryEmbeddingService, "format_context", selection)
    def submit(stream, text):
        list(stream(text, stop_fn=lambda: False))
        return 1
    controller.broker.submit = submit
    if image:
        controller.send_user_with_media("question", [{"type": "image", "data": "abc"}])
    else:
        controller._on_user_text("question")
    assert captured == [(2, .75)]
    content = "\n".join(m.content for m in client.requests[0]["messages"])
    assert "strong" in content and "weak" not in content and "global" in content
    assert len(controller._history) == 1
    session.load_user(7, "next", "user", {})
    assert panel._memory_maximum.value() == 5
    assert panel._memory_relevance.value() == .25
    panel.close()


def test_authorized_snapshot_with_changed_limits_preserves_admin_and_global():
    from test_memory_embeddings import db, user, FakeProvider
    from hamchat import db_ops
    conn = db()
    first, other, admin = user(conn, "first"), user(conn, "other"), user(conn, "admin", "admin")
    texts = {"mine": (first, "user", True), "other": (other, "user", True),
             "admin-only": (admin, "admin", True), "global": (admin, "global", True),
             "disabled": (first, "user", False)}
    provider = FakeProvider({text: [1., 0.] for text in [*texts, "q"]})
    service = MemoryEmbeddingService(conn, provider)
    for text, (owner, scope, enabled) in texts.items():
        mid = db_ops.create_memory(conn, owner_user_id=owner, content=text, scope=scope, enabled=enabled)
        service.embed_memory(mid)
    for uid, role, expected in ((first, "user", ["mine"]), (admin, "admin", ["admin-only"]), (None, "guest", [])):
        snapshot = service.snapshot_context(user_id=uid, role=role, conversation_id=None, profile_id=None)
        _, _, details = service.format_context("q", provider, snapshot, top_k=1, min_similarity=.99, return_details=True)
        assert details == {"semantic": expected, "managed": ["global"]}
