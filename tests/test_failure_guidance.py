from __future__ import annotations

import pytest

from hamchat.infra.llm.backend_adapter import make_stream_func_from_client
from hamchat.infra.llm.base import ChatMessage, ModelClient, StreamEvent
from hamchat.infra.llm.ollama_planner import RequestTooLargeError, plan_ollama_request
from hamchat.infra.llm.thread_broker import Job, _Worker
from hamchat.ui.chat_controller import ChatController


class LengthClient(ModelClient):
    def __init__(self, events):
        self.events = events

    def stream_chat(self, *, model, messages, options):
        yield from self.events


class FakeChat:
    def __init__(self):
        self.chunks = []

    def stream_chunk(self, row, text):
        self.chunks.append((row, text))


def test_preflight_rejection_uses_context_allocation_and_ham_mem_guidance():
    with pytest.raises(RequestTooLargeError) as raised:
        plan_ollama_request(
            messages=[ChatMessage(role="system", content="P" * 9000), ChatMessage(role="user", content="U")],
            options={},
            context_length=4096,
        )

    message = str(raised.value)
    assert "Increase Context allocation in Model → Model Manager" in message
    assert "temporarily disabling Use HamMem in the Chat Panel" in message
    assert "Thinking" not in message
    assert "older chat history" not in message


def test_length_terminal_is_a_distinct_output_limit_notice_and_keeps_partial_text():
    stream = make_stream_func_from_client(
        LengthClient([
            StreamEvent(type="delta", text="partial visible answer"),
            StreamEvent(type="end", finish_reason="length"),
        ]),
        model="test",
        build_messages=lambda prompt: [ChatMessage(role="user", content=prompt)],
    )
    worker = _Worker(Job(ticket=8, func=stream, args=("prompt",)))
    tokens, notices, finished = [], [], []
    worker.token.connect(lambda ticket, text: tokens.append((ticket, text)))
    worker.notice.connect(lambda ticket, kind, value: notices.append((ticket, kind, value)))
    worker.finished.connect(lambda ticket, status: finished.append((ticket, status)))
    worker.run()

    assert tokens == [(8, "partial visible answer")]
    assert notices == [(8, "output_limit", "")]
    assert finished == [(8, "ok")]


def test_output_limit_notice_is_visible_but_not_persisted_as_assistant_text():
    chat = FakeChat()
    state = type("ControllerState", (), {
        "_active_ticket": 8,
        "_active_row": 4,
        "_assistant_buf": ["partial visible answer"],
        "chat": chat,
    })()

    ChatController._on_job_notice(state, 8, "output_limit", "")

    assert state._assistant_buf == ["partial visible answer"]
    assert len(chat.chunks) == 1
    guidance = chat.chunks[0][1]
    assert "The model reached its output limit." in guidance
    assert "Any partial response has been kept." in guidance
    assert "If Thinking consumed the budget before an answer appeared" in guidance
    assert "lowering or disabling Thinking in the Chat Panel" in guidance


def test_thinking_only_length_exhaustion_still_has_a_visible_explanation():
    chat = FakeChat()
    state = type("ControllerState", (), {
        "_active_ticket": 9,
        "_active_row": 5,
        "_assistant_buf": [],
        "chat": chat,
    })()

    ChatController._on_job_notice(state, 9, "output_limit", "")

    assert "output limit" in chat.chunks[0][1]
    assert state._assistant_buf == []
