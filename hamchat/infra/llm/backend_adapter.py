# hamchat/infra/llm/backend_adapter.py
import logging
from typing import Callable, Iterator, List, Dict, Optional
from .base import ModelClient, ChatMessage, StreamEvent

MessagesBuilder = Callable[[str], List[ChatMessage]]
OptionsBuilder  = Callable[[], Dict]
log = logging.getLogger("llm.adapter")

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
        msgs   = build_messages(prompt)
        opts   = (build_options() if build_options else {}) or {}
        system_messages = [message for message in msgs if message.role == "system"]
        log.debug(
            "LLM adapter dispatch: client=%s roles=%s system_count=%d system_content_lengths=%s options=%s",
            type(client).__name__,
            [message.role for message in msgs],
            len(system_messages),
            [len(message.content or "") for message in system_messages],
            opts,
        )
        it     = client.stream_chat(model=model, messages=msgs, options=opts)
        for ev in it:
            if stop_fn():
                break
            if ev.type == "delta" and ev.text:
                yield ev.text
            elif ev.type == "error" and ev.error:
                # surface a short error fragment to the UI; broker will still emit finished()
                yield f"\n[error] {ev.error}\n"
                break
            elif ev.type == "end":
                break
        # generator ends naturally; worker will still call .close() if present
    return stream
