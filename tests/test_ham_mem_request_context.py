from __future__ import annotations

import numpy as np
from types import SimpleNamespace

import pytest

from hamchat.infra.llm.base import ChatMessage
from hamchat.infra.llm.ollama_client import OllamaClient, RuntimeContext
from hamchat.memory_embeddings import MemoryEmbeddingService
from hamchat.ui.chat_controller import ChatController


class FakeProvider:
    provider_id = "fake"; model_id = "embed"; format_version = 1; dimension = 2
    def embed_one(self, text): return np.array([1.0, 0.0], dtype=np.float32)
    def embed_many(self, texts): return [self.embed_one(text) for text in texts]


def test_request_context_order_is_ephemeral_and_backend_neutral():
    context, status = MemoryEmbeddingService.format_context(
        "question", FakeProvider(), ([(7, "semantic fact", .5, np.array([1., 0.], dtype=np.float32))], ["global rule"]))
    global_part, _, semantic_part = context.partition("\n\n[HamMem relevant memory]")
    profile = ChatMessage(role="system", content="profile prompt")
    final = [ChatMessage(role="system", content=global_part), profile,
             ChatMessage(role="system", content="[HamMem relevant memory]" + semantic_part),
             ChatMessage(role="user", content="question")]
    assert [m.content for m in final] == [global_part, "profile prompt", "[HamMem relevant memory]" + semantic_part, "question"]
    # Both clients receive the same logical role/content payload before their transport calls.
    ollama_wire = [{"role": m.role, "content": m.content} for m in final]
    openai_wire = [{"role": m.role, "content": m.content} for m in final]
    assert ollama_wire == openai_wire
    history = [ChatMessage(role="user", content="question")]
    assert all("HamMem" not in m.content for m in history)
    assert "1 relevant" in status and "1 global" in status


@pytest.mark.parametrize("large_first", [False, True])
def test_selected_datasheets_are_complete_beyond_former_pack_limit(large_first):
    first = "A" * (5000 if large_first else 3000)
    second = "B" * (300 if large_first else 3000)
    candidates = [(1, first, 1.0, np.array([1., 0.])),
                  (2, second, 0.5, np.array([1., 0.]))]

    context, status, details = MemoryEmbeddingService.format_context(
        "question", FakeProvider(), (candidates, []), return_details=True)

    assert details["semantic"] == [first, second]
    assert context.index(first) < context.index(second)
    assert "2 relevant selected" in status


def test_full_memory_prefix_reaches_text_and_image_ollama_payloads(monkeypatch):
    global_text = "G" * 4200
    first, second = "A" * 5100, "B" * 300
    snapshot = ([(1, first, 1.0, np.array([1., 0.])),
                 (2, second, 0.5, np.array([1., 0.]))], [global_text])
    statuses = []
    state = SimpleNamespace(
        _memory_provider=FakeProvider(),
        _session=SimpleNamespace(current=SimpleNamespace(role="admin")),
        _report_ham_mem=statuses.append,
    )
    prefix = ChatController._memory_prefix(state, "question", snapshot,
                                           ChatMessage("system", "profile prompt"))
    assert [item.role for item in prefix] == ["system", "system", "system"]
    assert prefix[0].content.endswith(f"- {global_text}\n")
    assert prefix[1].content == "profile prompt"
    assert first in prefix[2].content and second in prefix[2].content

    submitted = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def raise_for_status(self): return None
        def iter_lines(self, **_kwargs): return iter(['{"done":true}'])

    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.post",
                        lambda _url, **kwargs: (submitted.append(kwargs["json"]) or Response()))
    client = OllamaClient()
    for images in (False, True):
        user = ChatMessage("user", "question")
        if images:
            user.parts = [{"type": "image", "data_base64": "image-data"}]
        final = [*prefix, user]
        snapshots = []
        events = list(client.stream_chat(
            model="test", messages=final, options={},
            prepared_context=RuntimeContext(16384, "cache"),
            final_messages_callback=lambda messages: snapshots.append(
                ChatController._final_memory_snapshot(messages)),
        ))
        assert events[-1].type == "end"
        assert snapshots == [f"{prefix[0].content}\n\n{prefix[2].content}"]
        wire = submitted[-1]["messages"]
        assert [(item["role"], item["content"]) for item in wire] == [
            (item.role, item.content) for item in final]
        assert ("images" in wire[-1]) is images
    assert "request pending" in statuses[0]


def test_oversized_selected_memory_rejects_without_chat_request(monkeypatch):
    huge = "H" * 9000
    snapshot = ([(1, huge, 1.0, np.array([1., 0.]))], [])
    state = SimpleNamespace(_memory_provider=FakeProvider(),
                            _session=SimpleNamespace(current=SimpleNamespace(role="user")),
                            _report_ham_mem=lambda _status: None)
    prefix = ChatController._memory_prefix(state, "question", snapshot, None)
    calls = []
    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.post",
                        lambda *_args, **_kwargs: calls.append(True))
    snapshots = []
    events = list(OllamaClient().stream_chat(
        model="test", messages=[*prefix, ChatMessage("user", "question")], options={},
        prepared_context=RuntimeContext(4096, "cache"),
        final_messages_callback=lambda messages: snapshots.append(messages),
    ))
    assert [event.type for event in events] == ["error"]
    assert "Increase Context allocation" in events[0].error
    assert calls == [] and snapshots == []
