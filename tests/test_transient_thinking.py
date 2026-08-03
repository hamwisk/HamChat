from __future__ import annotations

from types import SimpleNamespace

from hamchat.infra.llm.backend_adapter import make_stream_func_from_client
from hamchat.infra.llm.base import ChatMessage, ModelClient, StreamEvent
from hamchat.infra.llm.thread_broker import Job, StreamChunk, _Worker
from hamchat.ui.chat_controller import ChatController, HistoryEntry


class EventClient(ModelClient):
    def __init__(self, events):
        self.events = events

    def stream_chat(self, *, model, messages, options):
        yield from self.events


class ThinkingPanel:
    def __init__(self):
        self.text = ""
        self.clear_count = 0
        self.collapsed = True

    def clear_thinking(self):
        self.clear_count += 1
        self.text = ""

    def append_thinking(self, text):
        self.text += text


class FakeChat:
    def __init__(self):
        self.streamed = []
        self.ended = []
        self.streaming = []

    def stream_chunk(self, row, text):
        self.streamed.append((row, text))

    def end_assistant_stream(self, row):
        self.ended.append(row)

    def set_streaming(self, value):
        self.streaming.append(value)


class ControllerState:
    _clear_thinking = ChatController._clear_thinking
    clear_transient_thinking = ChatController.clear_transient_thinking
    _refresh_thinking_controls = lambda self: None


def _stream_for(events):
    return make_stream_func_from_client(
        EventClient(events),
        model="test",
        build_messages=lambda prompt: [ChatMessage(role="user", content=prompt)],
    )


def test_thinking_and_visible_chunks_use_separate_worker_channels_in_order():
    stream = _stream_for([
        StreamEvent(type="thinking", text="first "),
        StreamEvent(type="thinking", text="second"),
        StreamEvent(type="delta", text="answer"),
        StreamEvent(type="end"),
    ])
    worker = _Worker(Job(ticket=9, func=stream, args=("hello",)))
    thinking, visible = [], []
    worker.thinking.connect(lambda ticket, text: thinking.append((ticket, text)))
    worker.token.connect(lambda ticket, text: visible.append((ticket, text)))

    worker.run()

    assert thinking == [(9, "first second")]
    assert visible == [(9, "answer")]


def test_visible_only_models_do_not_create_thinking_updates():
    worker = _Worker(Job(
        ticket=2,
        func=_stream_for([StreamEvent(type="delta", text="answer"), StreamEvent(type="end")]),
        args=("hello",),
    ))
    thinking, visible = [], []
    worker.thinking.connect(lambda *_args: thinking.append(_args))
    worker.token.connect(lambda *_args: visible.append(_args))

    worker.run()

    assert thinking == []
    assert visible == [(2, "answer")]


def test_controller_keeps_thinking_out_of_assistant_buffer_and_rejects_stale_chunks():
    panel = ThinkingPanel()
    state = SimpleNamespace(
        _thinking_panel=panel,
        _active_ticket=7,
        _thinking_ticket=7,
        _assistant_buf=["already visible"],
    )

    ChatController._on_job_thinking(state, 6, "old")
    ChatController._on_job_thinking(state, 7, "reasoning")

    assert panel.text == "reasoning"
    assert state._assistant_buf == ["already visible"]


def test_thinking_lifecycle_clears_only_for_new_generation_or_conversation_switch():
    panel = ThinkingPanel()
    state = ControllerState()
    state._thinking_panel = panel
    state._history = [HistoryEntry(None, ChatMessage(role="assistant", content="answer"))]
    state._assistant_buf = ["partial answer"]
    state._conv_id = 10

    panel.append_thinking("completed reasoning")
    ChatController._begin_thinking_generation(state)
    assert panel.text == ""

    # Completion/error does not clear inspectable partial thinking.
    panel.append_thinking("partial reasoning")
    state._active_ticket = 1
    state._active_row = 0
    state.chat = FakeChat()
    ChatController._on_job_error(state, 1, "interrupted")
    assert panel.text == "partial reasoning"
    ChatController._on_job_finished(state, 1, "cancelled")
    assert panel.text == "partial reasoning"

    ChatController.reset_history(state)
    assert panel.text == ""
    panel.append_thinking("other conversation")
    ChatController.load_conversation(state, 20, [])
    assert panel.text == ""


def test_thinking_is_not_reconstructed_into_history_or_visible_chat_rows():
    panel = ThinkingPanel()
    chat = FakeChat()
    state = SimpleNamespace(
        _thinking_panel=panel,
        _active_ticket=4,
        _thinking_ticket=4,
        _active_row=3,
        _assistant_buf=[],
        chat=chat,
        _history=[],
    )

    ChatController._on_job_thinking(state, 4, "hidden")
    ChatController._on_job_token(state, 4, "shown")

    assert panel.text == "hidden"
    assert state._assistant_buf == ["shown"]
    assert chat.streamed == [(3, "shown")]
    assert all(entry.msg.content != "hidden" for entry in state._history)


def test_collapsed_thinking_panel_does_not_change_stream_delivery():
    panel = ThinkingPanel()
    panel.collapsed = True
    state = SimpleNamespace(_thinking_panel=panel, _active_ticket=3, _thinking_ticket=3)

    ChatController._on_job_thinking(state, 3, "still streamed")

    assert panel.collapsed is True
    assert panel.text == "still streamed"


def test_cancelled_ticket_rejects_late_thinking_without_clearing_partial_buffer():
    panel = ThinkingPanel()
    panel.append_thinking("partial")
    state = SimpleNamespace(_thinking_panel=panel, _active_ticket=8, _thinking_ticket=-1)

    ChatController._on_job_thinking(state, 8, " late")

    assert panel.text == "partial"
