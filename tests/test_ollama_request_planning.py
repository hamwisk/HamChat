from __future__ import annotations

import logging

import pytest

from hamchat.infra.llm.backend_adapter import make_stream_func_from_client
from hamchat.infra.llm.base import ChatMessage, ModelClient, StreamEvent
from hamchat.infra.llm.ollama_client import OllamaClient, RuntimeContext
from hamchat.infra.llm.ollama_planner import (
    DEFAULT_NUM_PREDICT,
    IMAGE_TOKEN_ALLOWANCE,
    RequestTooLargeError,
    estimate_message_tokens,
    plan_ollama_request,
)
from hamchat.infra.llm.thread_broker import Job, _Worker


def message(role: str, content: str, images: int = 0) -> ChatMessage:
    item = ChatMessage(role=role, content=content)
    if images:
        setattr(item, "parts", [{"type": "image", "data_base64": "image-data"}] * images)
    return item


def plan(messages, context=8192, options=None):
    return plan_ollama_request(messages=messages, options=options or {}, context_length=context)


def test_short_request_passes_unchanged_and_sets_default_num_predict():
    messages = [message("system", "profile"), message("user", "hello")]

    result = plan(messages, context=8192)

    assert result.messages == messages
    assert result.options["num_predict"] == DEFAULT_NUM_PREDICT
    assert result.outcome == "fit"


def test_system_profile_and_ham_mem_are_included_in_estimate():
    profile = message("system", "P" * 100)
    ham_mem = message("system", "M" * 200)
    result = plan([profile, ham_mem, message("user", "hello")])

    assert result.original_input_tokens >= 150
    assert len(result.messages) == 3


def test_oldest_complete_turn_is_trimmed_first_and_no_message_is_cut():
    messages = [
        message("system", "profile"),
        message("user", "u1" * 500), message("assistant", "a1" * 500),
        message("user", "u2" * 500), message("assistant", "a2" * 500),
        message("user", "new" * 40),
    ]

    # Input plus the requested 4096-token allowance fits after one old turn goes.
    result = plan(messages, context=5300)

    assert result.outcome == "trimmed"
    assert result.omitted_turn_count == 1
    assert [item.content for item in result.messages] == [
        "profile", "u2" * 500, "a2" * 500, "new" * 40,
    ]


def test_repeated_trimming_removes_multiple_complete_pairs_without_orphans():
    messages = [
        message("system", "profile"),
        message("user", "u1" * 500), message("assistant", "a1" * 500),
        message("user", "u2" * 500), message("assistant", "a2" * 500),
        message("user", "new" * 40),
    ]

    result = plan(messages, context=4300)

    assert result.omitted_turn_count == 2
    assert [(item.role, item.content) for item in result.messages] == [
        ("system", "profile"), ("user", "new" * 40),
    ]
    assert not any(item.role == "assistant" for item in result.messages)


def test_newest_user_system_messages_and_attachments_always_survive():
    newest = message("user", "newest", images=2)
    result = plan([
        message("system", "profile"), message("system", "[HamMem]"),
        message("user", "old" * 1000), message("assistant", "reply" * 1000), newest,
    ], context=6500)

    assert result.messages[0].content == "profile"
    assert result.messages[1].content == "[HamMem]"
    assert result.messages[-1] is newest
    assert getattr(result.messages[-1], "parts") == getattr(newest, "parts")


def test_image_messages_have_nonzero_allowance_and_stay_with_parent():
    plain = message("user", "same")
    image = message("user", "same", images=1)

    assert estimate_message_tokens(image) - estimate_message_tokens(plain) == IMAGE_TOKEN_ALLOWANCE
    assert plan([message("system", "p"), image]).messages[-1] is image


def test_attachment_history_stub_is_trimmed_with_its_parent_turn():
    parent = message("user", "old" * 1000)
    stub = ChatMessage(role="user", content="[User attached 1 image]", metadata={"attachment_stub_parent": True})
    result = plan([
        message("system", "profile"), parent, stub, message("assistant", "reply" * 1000),
        message("user", "new"),
    ], context=4500)

    assert parent not in result.messages
    assert stub not in result.messages


@pytest.mark.parametrize("chars,should_fit", [(7014, True), (7016, True), (7018, False)])
def test_mandatory_request_budget_boundaries(chars, should_fit):
    messages = [message("user", "x" * chars)]
    if should_fit:
        assert plan(messages, context=4096, options={"num_predict": 512}).messages == messages
    else:
        with pytest.raises(RequestTooLargeError):
            plan(messages, context=4096, options={"num_predict": 512})


def test_required_system_and_newest_prompt_too_large_prevents_chat_post(monkeypatch):
    posts = []
    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.post",
        lambda *args, **kwargs: posts.append((args, kwargs)),
    )
    client = OllamaClient()
    huge = [message("system", "P" * 6000), message("user", "U" * 6000)]

    events = list(client.stream_chat(
        model="test", messages=huge, options={},
        prepared_context=RuntimeContext(4096, "runtime"), request_id="request1",
    ))

    assert events[-1].type == "error"
    assert "cannot fit" in events[-1].error
    assert posts == []


def test_rejected_plan_reaches_broker_as_error_without_emitting_tokens():
    client = OllamaClient()
    stream_func = make_stream_func_from_client(
        client,
        model="test",
        build_messages=lambda _prompt: [message("user", "x" * 10000)],
        build_options=lambda: {},
    )
    # Avoid network preparation while retaining the same request-too-large path.
    client.prepare_runtime_context = lambda **_kwargs: RuntimeContext(4096, "fallback")
    worker = _Worker(Job(ticket=1, func=stream_func, args=("x",)))
    tokens, errors, finished = [], [], []
    worker.token.connect(lambda *_args: tokens.append(_args))
    worker.error.connect(lambda *_args: errors.append(_args))
    worker.finished.connect(lambda *_args: finished.append(_args))
    worker.run()

    assert tokens == []
    assert errors and "cannot fit" in errors[0][1]
    assert finished == [(1, "error")]


def test_num_predict_default_and_small_override_preserved_excess_rejected():
    messages = [message("user", "hello")]
    default = plan(messages, context=8192)
    smaller = plan(messages, options={"num_predict": 128})
    with pytest.raises(RequestTooLargeError):
        plan(messages, options={"num_predict": 999999})

    assert default.num_predict == DEFAULT_NUM_PREDICT
    assert smaller.num_predict == 128


def test_exact_planned_messages_and_options_are_submitted(monkeypatch):
    submitted = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def raise_for_status(self): return None
        def iter_lines(self, **_kwargs): return iter(['{"done":true}'])

    monkeypatch.setattr(
        "hamchat.infra.llm.ollama_client.requests.post",
        lambda _url, **kwargs: (submitted.append(kwargs["json"]) or Response()),
    )
    messages = [
        message("system", "profile"), message("user", "old" * 1000),
        message("assistant", "answer" * 1000), message("user", "new", images=1),
    ]
    client = OllamaClient()
    events = list(client.stream_chat(
        model="test", messages=messages, options={},
        prepared_context=RuntimeContext(6000, "cache"), request_id="request2",
    ))

    assert events[-1].type == "end"
    assert [item["content"] for item in submitted[0]["messages"]] == ["profile", "new"]
    assert "images" in submitted[0]["messages"][-1]
    assert submitted[0]["options"]["num_predict"] > 0


@pytest.mark.parametrize("source", ["runtime", "cache", "fallback"])
def test_all_runtime_context_sources_plan_requests(monkeypatch, source):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def raise_for_status(self): return None
        def iter_lines(self, **_kwargs): return iter(['{"done":true}'])

    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.post", lambda *_a, **_k: Response())
    events = list(OllamaClient().stream_chat(
        model="test", messages=[message("user", "hello")], options={},
        prepared_context=RuntimeContext(8192, source), request_id="request-source",
    ))

    assert events[-1].type == "end"


def test_plan_logging_has_budgets_and_never_message_contents(monkeypatch, caplog):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def raise_for_status(self): return None
        def iter_lines(self, **_kwargs): return iter(['{"done":true}'])

    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.post", lambda *_a, **_k: Response())
    caplog.set_level(logging.INFO, logger="llm.ollama")
    secret = "do-not-log-this-request-content"
    list(OllamaClient().stream_chat(
        model="test", messages=[message("user", secret)], options={},
        prepared_context=RuntimeContext(8192, "runtime"), request_id="request3",
    ))

    assert "original_input_tokens=" in caplog.text
    assert "num_predict=" in caplog.text
    assert secret not in caplog.text


class NonOllamaClient(ModelClient):
    def __init__(self): self.seen = None
    def stream_chat(self, *, model, messages, options):
        self.seen = (model, messages, options)
        yield StreamEvent(type="end")


def test_non_ollama_adapter_path_remains_unchanged():
    client = NonOllamaClient()
    messages = [message("user", "hello")]
    stream_func = make_stream_func_from_client(
        client, model="remote", build_messages=lambda _prompt: messages,
        build_options=lambda: {"temperature": 0.3},
    )

    assert list(stream_func("hello", stop_fn=lambda: False)) == []
    assert client.seen == ("remote", messages, {"temperature": 0.3})


def test_generation_has_priority_over_removable_history():
    messages = [message("system", "profile"), message("user", "old" * 1000),
                message("assistant", "answer" * 300), message("user", "new")]
    original = list(messages)
    result = plan(messages, context=5000)
    assert result.messages == [messages[0], messages[-1]]
    assert result.requested_num_predict == result.num_predict == 4096
    assert result.omitted_turn_count == 1
    assert messages == original


@pytest.mark.parametrize("invalid", [0, -1, -2, True, "8192", 3.5])
def test_invalid_generation_override_falls_back_to_finite_default(invalid):
    result = plan([message("user", "hi")], context=16384, options={"num_predict": invalid})
    assert result.requested_num_predict == result.num_predict == 4096


def test_omission_notice_and_log_report_preserved_shared_budget(monkeypatch, caplog):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def raise_for_status(self): return None
        def iter_lines(self, **_kwargs): return iter(['{"done":true}'])

    submitted = []
    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.post",
                        lambda _url, **kw: submitted.append(kw["json"]) or Response())
    caplog.set_level(logging.INFO, logger="llm.ollama")
    events = list(OllamaClient().stream_chat(
        model="test", messages=[message("system", "M" * 3000), message("user", "old" * 3000),
                               message("assistant", "reply"), message("user", "hi")],
        options={"num_predict": 7000}, prepared_context=RuntimeContext(10000, "cache")))
    notice = next(event for event in events if event.type == "generation_budget")
    effective = submitted[0]["options"]["num_predict"]
    assert effective == 7000
    assert "Excluded 1 oldest turns (2 messages)" in notice.text
    assert "shared by Thinking and answer" in notice.text
    assert "requested_num_predict=7000" in caplog.text
    assert "effective_num_predict=7000" in caplog.text
    assert "omitted_turn_count=1" in caplog.text
    assert "reasoning_reserve" not in caplog.text


def test_observed_602_token_headroom_omits_history_instead_of_shrinking_output():
    messages = [message("system", "S" * 2500) for _ in range(4)]
    for _ in range(10):
        messages.extend([message("user", "U" * 1000), message("assistant", "A" * 1000)])
    messages.append(message("user", "N" * 836))
    original = list(messages)
    result = plan(messages, context=16384)
    assert len(messages) == 25
    assert result.original_input_tokens == 15418
    assert 16384 - result.original_input_tokens - (64 + 25 * 12) == 602
    assert result.num_predict == result.requested_num_predict == 4096
    assert result.omitted_turn_count == 4
    assert result.messages == messages[:4] + messages[12:]
    assert result.final_input_tokens + result.template_reserve + 4096 <= 16384
    assert messages == original


@pytest.mark.parametrize("retained_turns", [0, 1, 2, 3, 4])
def test_recent_turns_are_preferred_without_being_a_fixed_history_limit(retained_turns):
    messages = [message("system", "s")]
    for index in range(4):
        messages.extend([message("user", str(index) * 200),
                         message("assistant", str(index) * 200)])
    messages.append(message("user", "n"))
    # Mandatory text = 2, template = 88, generation = 128;
    # each completed turn adds 200 input tokens and 24 wrapper tokens.
    result = plan(messages, context=218 + 224 * retained_turns, options={"num_predict": 128})
    assert result.messages == messages[:1] + messages[1 + (4 - retained_turns) * 2:]
    assert result.omitted_turn_count == 4 - retained_turns
    assert result.num_predict == 128


def test_newest_text_and_attachment_stub_are_both_mandatory():
    newest = message("user", "N" * 1000)
    stub = ChatMessage("user", "attachment", metadata={"attachment_stub_parent": True})
    with pytest.raises(RequestTooLargeError):
        plan([message("user", "old"), message("assistant", "reply"), newest, stub],
             context=400, options={"num_predict": 128})


@pytest.mark.parametrize("images", [0, 1])
def test_mandatory_input_alone_fits_but_requested_output_does_not_rejects(monkeypatch, caplog, images):
    posts, snapshots = [], []
    monkeypatch.setattr("hamchat.infra.llm.ollama_client.requests.post",
                        lambda *a, **kw: posts.append(kw))
    messages = [message("system", "profile"), message("system", "memory" * 100),
                message("user", "question", images=images)]
    caplog.set_level(logging.WARNING, logger="llm.ollama")
    events = list(OllamaClient().stream_chat(
        model="test", messages=messages, options={"num_predict": 4096},
        prepared_context=RuntimeContext(4096, "runtime"),
        final_messages_callback=snapshots.append))
    assert [event.type for event in events] == ["error"]
    assert "Preflight context rejection" in events[0].error
    assert "Lower Maximum generation" in events[0].error
    assert "requested 4096-token" in events[0].error
    assert "effective_num_predict=none" in caplog.text
    assert not posts and not snapshots
