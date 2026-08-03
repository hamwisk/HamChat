# hamchat/infra/llm/backend_adapter.py
import uuid
from typing import Callable, Iterator, List, Dict, Optional
from .base import ModelClient, ChatMessage, StreamError

MessagesBuilder = Callable[[str], List[ChatMessage]]
OptionsBuilder  = Callable[[], Dict]

def make_stream_func_from_client(
    client: ModelClient,
    *,
    model: str,
    build_messages: MessagesBuilder,
    build_options: Optional[OptionsBuilder] = None,
) -> Callable[..., Iterator[str]]:
    """
    Returns a StreamFunc(prompt, *, stop_fn) -> Iterator[str]
    that the ThreadBroker can schedule.
    """
    def stream(prompt: str, *, stop_fn) -> Iterator[str]:
        opts   = (build_options() if build_options else {}) or {}
        prepare_runtime_context = getattr(client, "prepare_runtime_context", None)
        request_id = uuid.uuid4().hex[:8] if callable(prepare_runtime_context) else None
        prepared_context = (
            prepare_runtime_context(model=model, options=opts, request_id=request_id)
            if callable(prepare_runtime_context) else None
        )
        msgs = build_messages(prompt)
        if prepared_context is not None:
            it = client.stream_chat(
                model=model, messages=msgs, options=opts,
                prepared_context=prepared_context, request_id=request_id,
            )
        else:
            it = client.stream_chat(model=model, messages=msgs, options=opts)
        for ev in it:
            if stop_fn():
                break
            if ev.type == "delta" and ev.text:
                yield ev.text
            elif ev.type == "error":
                raise StreamError(ev.error or "LLM stream failed")
            elif ev.type == "end":
                break
        # generator ends naturally; worker will still call .close() if present
    return stream
