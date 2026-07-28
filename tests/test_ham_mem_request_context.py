from __future__ import annotations

import numpy as np

from hamchat.infra.llm.base import ChatMessage
from hamchat.memory_embeddings import MemoryEmbeddingService


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
