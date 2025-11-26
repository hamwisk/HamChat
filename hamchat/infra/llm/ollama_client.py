# hamchat/infra/llm/ollama_client.py
from __future__ import annotations
import json, requests
from typing import Iterator, List, Dict
from .base import ModelClient, ChatMessage, StreamEvent

DEFAULT_OLLAMA = "http://127.0.0.1:11434"  # mirrors your registry default

class OllamaClient(ModelClient):
    def __init__(self, base_url: str = DEFAULT_OLLAMA, timeout: int = 269):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def stream_chat(self, *, model: str, messages: List[ChatMessage], options: Dict) -> Iterator[StreamEvent]:
        # Build wire messages; translate our internal .parts → Ollama's "images"
        wire = []
        for m in messages:
            md = {
                "role": getattr(m, "role", "user"),
                "content": getattr(m, "content", "") or "",
            }
            parts = getattr(m, "parts", None)
            if parts:
                images = []
                for p in parts:
                    # Accept our helper's dict format, or a raw base64 string just in case
                    if isinstance(p, dict) and (p.get("type") == "image"):
                        b64 = p.get("data_base64") or p.get("image") or p.get("data")
                        if b64:
                            images.append(b64)
                    elif isinstance(p, str) and p.strip():
                        images.append(p.strip())
                if images:
                    md["images"] = images
            wire.append(md)

        payload = {
            "model": model,
            "messages": wire,
            "stream": True,
            "options": options or {},
        }
        url = f"{self.base_url}/api/chat"
        try:
            with requests.post(url, json=payload, stream=True, timeout=self.timeout) as r:
                r.raise_for_status()
                yield StreamEvent(type="start")
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    # each line is a JSON object { "message": {"role": "...", "content": "Δ"}, "done": bool, ...}
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get("error"):
                        yield StreamEvent(type="error", error=str(obj["error"]))
                        break
                    msg = (obj.get("message") or {})
                    delta = msg.get("content") or ""
                    if delta:
                        yield StreamEvent(type="delta", text=delta)
                    if obj.get("done"):
                        yield StreamEvent(
                            type="end",
                            finish_reason=(obj.get("done_reason") or None),
                            usage={k: obj.get(k) for k in ("prompt_eval_count", "eval_count", "total_duration")}
                        )
                        break
        except Exception as e:
            yield StreamEvent(type="error", error=str(e))
