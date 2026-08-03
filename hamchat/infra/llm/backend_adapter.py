# hamchat/infra/llm/backend_adapter.py
import time
import uuid
from typing import Callable, Iterator, List, Dict, Optional
from .base import ModelClient, ChatMessage, StreamError
from .thread_broker import StreamChunk

MessagesBuilder = Callable[[str], List[ChatMessage]]
OptionsBuilder  = Callable[[], Dict]
ThinkingBuilder = Callable[[], Optional[str]]

def make_stream_func_from_client(
    client: ModelClient,
    *,
    model: str,
    build_messages: MessagesBuilder,
    build_options: Optional[OptionsBuilder] = None,
    build_thinking: Optional[ThinkingBuilder] = None,
    build_requested_thinking: Optional[ThinkingBuilder] = None,
) -> Callable[..., Iterator[str]]:
    """
    Returns a StreamFunc(prompt, *, stop_fn) -> Iterator[str]
    that the ThreadBroker can schedule.
    """
    def stream(prompt: str, *, stop_fn) -> Iterator[str]:
        opts   = (build_options() if build_options else {}) or {}
        thinking_mode = build_thinking() if build_thinking else None
        requested_thinking_mode = build_requested_thinking() if build_requested_thinking else thinking_mode
        prepare_runtime_context = getattr(client, "prepare_runtime_context", None)
        request_id = uuid.uuid4().hex[:8] if callable(prepare_runtime_context) else None
        prepared_context = (
            prepare_runtime_context(model=model, options=opts, request_id=request_id)
            if callable(prepare_runtime_context) else None
        )
        msgs = build_messages(prompt)
        if prepared_context is not None:
            stream_kwargs = dict(
                model=model, messages=msgs, options=opts,
                prepared_context=prepared_context, request_id=request_id,
            )
            if thinking_mode is not None:
                stream_kwargs["thinking_mode"] = thinking_mode
                stream_kwargs["requested_thinking_mode"] = requested_thinking_mode
            it = client.stream_chat(**stream_kwargs)
        else:
            stream_kwargs = dict(model=model, messages=msgs, options=opts)
            if thinking_mode is not None:
                stream_kwargs["thinking_mode"] = thinking_mode
                stream_kwargs["requested_thinking_mode"] = requested_thinking_mode
            it = client.stream_chat(**stream_kwargs)
        thinking_parts: list[str] = []
        last_thinking_flush = time.monotonic()

        def flush_thinking() -> Iterator[StreamChunk]:
            nonlocal last_thinking_flush
            if thinking_parts:
                text = "".join(thinking_parts)
                thinking_parts.clear()
                last_thinking_flush = time.monotonic()
                yield StreamChunk(type="thinking", text=text)

        for ev in it:
            if stop_fn():
                break
            if ev.type == "thinking" and ev.text:
                thinking_parts.append(ev.text)
                if time.monotonic() - last_thinking_flush >= 0.05:
                    yield from flush_thinking()
            elif ev.type in {"thinking_forced_low", "thinking_rejected"}:
                yield StreamChunk(type=ev.type, text=ev.text)
            elif ev.type == "delta" and ev.text:
                # Thinking and visible text have independent destinations.
                # Do not turn an interleaved visible-token stream into one GUI
                # event per thinking token; flush on the short cadence above.
                if time.monotonic() - last_thinking_flush >= 0.05:
                    yield from flush_thinking()
                yield ev.text
            elif ev.type == "error":
                yield from flush_thinking()
                raise StreamError(ev.error or "LLM stream failed")
            elif ev.type == "end":
                yield from flush_thinking()
                break
        yield from flush_thinking()
        # generator ends naturally; worker will still call .close() if present
    return stream
