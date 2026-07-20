# hamchat/infra/llm/openai_client.py
from __future__ import annotations
from typing import Iterator, List, Dict, Optional
from openai import OpenAI

from .base import ModelClient, ChatMessage, StreamEvent

class OpenAIClient(ModelClient):
    def __init__(self, api_key: str | None = None, timeout: int = 120):
        # If api_key is None, rely on OPENAI_API_KEY env var.
        if api_key is not None:
            self.client = OpenAI(api_key=api_key)
        else:
            self.client = OpenAI()
        self.timeout = timeout

    def stream_chat(self, *, model: str, messages: List[ChatMessage], options: Dict) -> Iterator[StreamEvent]:
        wire_messages = [
            {"role": m.role, "content": m.content}
            for m in messages
        ]

        # You can merge options into the call (temperature, etc.)
        stream = self.client.chat.completions.create(
            model=model,
            messages=wire_messages,
            stream=True,
            **(options or {}),
        )

        yield StreamEvent(type="start")
        try:
            for chunk in stream:
                choice = getattr(chunk, "choices", [None])[0]
                if not choice:
                    continue
                delta = getattr(choice, "delta", None)
                text = getattr(delta, "content", "") or ""
                if text:
                    yield StreamEvent(type="delta", text=text)
                if getattr(choice, "finish_reason", None):
                    # usage may only be in final response if you do non-streaming;
                    # you can leave usage=None here for now.
                    yield StreamEvent(type="end", finish_reason=choice.finish_reason)
                    break
        except Exception as exc:
            yield StreamEvent(type="error", error=str(exc))
