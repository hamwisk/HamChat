# hamchat/infra/llm/base.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Iterator, List, Dict, Optional

@dataclass
class ChatMessage:
    role: str   # "user" | "assistant" | "system"
    content: str
    metadata: dict | None = None


@dataclass
class StreamEvent:
    type: str               # "start" | "delta" | "end" | "error"
    text: str = ""
    finish_reason: Optional[str] = None
    usage: Optional[Dict] = None
    error: Optional[str] = None


class ModelClient:
    """Abstract client."""
    def stream_chat(self, *, model: str, messages: List[ChatMessage], options: Dict) -> Iterator[StreamEvent]:
        raise NotImplementedError
