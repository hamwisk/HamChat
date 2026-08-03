# hamchat/infra/llm/ollama_client.py
from __future__ import annotations
import json
import logging
import time
import uuid
import requests
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple
from .base import ModelClient, ChatMessage, StreamEvent

DEFAULT_OLLAMA = "http://127.0.0.1:11434"  # mirrors your registry default
FALLBACK_CONTEXT_LENGTH = 4096
PREPARATION_CONNECT_TIMEOUT = 5
PREPARATION_READ_TIMEOUT = 300
CONTEXT_OPTION_KEYS = frozenset({
    "num_ctx", "num_keep", "rope_frequency_base", "rope_frequency_scale", "rope_scaling",
})
log = logging.getLogger("llm.ollama")


@dataclass(frozen=True)
class RuntimeContext:
    """The effective context for an active Ollama runtime."""

    context_length: int
    source: str  # "runtime" | "cache" | "fallback"


class OllamaClient(ModelClient):
    def __init__(self, base_url: str = DEFAULT_OLLAMA, timeout: int = 269):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._runtime_context_cache: Dict[Tuple[str, str, Tuple[Tuple[str, str], ...]], int] = {}

    def stream_chat(
        self,
        *,
        model: str,
        messages: List[ChatMessage],
        options: Dict,
        prepared_context: Optional[RuntimeContext] = None,
        request_id: Optional[str] = None,
    ) -> Iterator[StreamEvent]:
        request_id = request_id or uuid.uuid4().hex[:8]
        effective_options = dict(options or {})

        # Build wire messages; translate our internal .parts → Ollama's "images".
        wire = []
        image_count = 0
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
                    image_count += len(images)
            wire.append(md)

        text_char_count = sum(len(message["content"] or "") for message in wire)
        # This deliberately coarse estimate is diagnostic-only; it is not a tokenizer.
        approx_input_token_count = (text_char_count + 3) // 4
        requested_num_ctx = effective_options.get("num_ctx")
        resolved_context = prepared_context or self.prepare_runtime_context(
            model=model, options=effective_options, request_id=request_id,
        )
        log.info(
            "Ollama request request_id=%s model=%s options=%s message_count=%d "
            "text_char_count=%d image_count=%d approx_input_token_count=%d "
            "requested_num_ctx=%r effective_context_length=%d effective_context_source=%s",
            request_id, model, effective_options, len(wire), text_char_count,
            image_count, approx_input_token_count, requested_num_ctx,
            resolved_context.context_length, resolved_context.source,
        )

        payload = {
            "model": model,
            "messages": wire,
            "stream": True,
            "options": effective_options,
        }
        url = f"{self.base_url}/api/chat"

        line_number = 0
        thinking_chunk_count = thinking_char_count = 0
        visible_chunk_count = visible_char_count = 0
        malformed_line_count = 0
        stream_compromised = False
        terminal_received = False
        try:
            with requests.post(url, json=payload, stream=True, timeout=self.timeout) as r:
                r.raise_for_status()
                log.debug("Ollama stream opened request_id=%s", request_id)
                yield StreamEvent(type="start")
                for line in r.iter_lines(decode_unicode=True):
                    line_number += 1
                    if not line:
                        continue
                    # each line is a JSON object { "message": {"role": "...", "content": "Δ"}, "done": bool, ...}
                    try:
                        obj = json.loads(line)
                        if not isinstance(obj, dict):
                            raise ValueError("NDJSON line is not an object")
                    except Exception as exc:
                        malformed_line_count += 1
                        stream_compromised = True
                        safe_chunk = repr(line)
                        if len(safe_chunk) > 300:
                            safe_chunk = safe_chunk[:297] + "..."
                        log.warning(
                            "Ollama malformed NDJSON request_id=%s line_number=%d "
                            "exception_type=%s chunk=%s",
                            request_id, line_number, type(exc).__name__, safe_chunk,
                        )
                        continue
                    if obj.get("error"):
                        error = str(obj["error"])
                        log.warning(
                            "Ollama stream error request_id=%s line_number=%d error=%s",
                            request_id, line_number, error,
                        )
                        yield StreamEvent(type="error", error=f"Ollama error [{request_id}]: {error}")
                        return
                    message_obj = obj.get("message")
                    msg = message_obj if isinstance(message_obj, dict) else {}
                    thinking = msg.get("thinking") or ""
                    if thinking:
                        thinking_chunk_count += 1
                        thinking_char_count += len(thinking)
                    delta = msg.get("content") or ""
                    if delta:
                        visible_chunk_count += 1
                        visible_char_count += len(delta)
                        yield StreamEvent(type="delta", text=delta)
                    if obj.get("done") is True:
                        terminal_received = True
                        usage = {k: obj.get(k) for k in ("prompt_eval_count", "eval_count", "total_duration")}
                        log.info(
                            "Ollama terminal request_id=%s model=%s done=%s done_reason=%r "
                            "prompt_eval_count=%r eval_count=%r total_duration=%r "
                            "thinking_chunk_count=%d thinking_char_count=%d "
                            "visible_chunk_count=%d visible_char_count=%d malformed_line_count=%d",
                            request_id, obj.get("model") or model, obj.get("done"), obj.get("done_reason"),
                            usage["prompt_eval_count"], usage["eval_count"], usage["total_duration"],
                            thinking_chunk_count, thinking_char_count, visible_chunk_count,
                            visible_char_count, malformed_line_count,
                        )
                        if stream_compromised:
                            yield StreamEvent(
                                type="error",
                                error=(f"Ollama protocol error [{request_id}]: malformed NDJSON "
                                       f"received in stream ({malformed_line_count} line(s))"),
                            )
                        else:
                            yield StreamEvent(
                                type="end", finish_reason=(obj.get("done_reason") or None), usage=usage,
                            )
                        return
                if not terminal_received:
                    reason = "malformed NDJSON" if stream_compromised else "stream ended before terminal done=true"
                    log.warning(
                        "Ollama interrupted stream request_id=%s reason=%s line_count=%d "
                        "malformed_line_count=%d thinking_chunk_count=%d thinking_char_count=%d "
                        "visible_chunk_count=%d visible_char_count=%d",
                        request_id, reason, line_number, malformed_line_count,
                        thinking_chunk_count, thinking_char_count, visible_chunk_count,
                        visible_char_count,
                    )
                    self.invalidate_runtime_context()
                    yield StreamEvent(type="error", error=f"Ollama interrupted stream [{request_id}]: {reason}")
        except Exception as e:
            self.invalidate_runtime_context()
            log.warning(
                "Ollama request failed request_id=%s exception_type=%s error=%s",
                request_id, type(e).__name__, e,
            )
            yield StreamEvent(type="error", error=f"Ollama request failed [{request_id}]: {e}")

    def prepare_runtime_context(
        self,
        *,
        model: str,
        options: Optional[Dict] = None,
        request_id: Optional[str] = None,
    ) -> RuntimeContext:
        """Preload a model and discover its runtime context before real chat work."""
        request_id = request_id or uuid.uuid4().hex[:8]
        effective_options = dict(options or {})
        cache_key = self._runtime_context_cache_key(model, effective_options)
        cached = self._runtime_context_cache.get(cache_key)
        if cached is not None:
            return RuntimeContext(context_length=cached, source="cache")

        preload_started = time.monotonic()
        try:
            preload = requests.post(
                f"{self.base_url}/api/generate",
                json={"model": model, "prompt": "", "stream": False},
                timeout=(PREPARATION_CONNECT_TIMEOUT, PREPARATION_READ_TIMEOUT),
            )
            preload.raise_for_status()
            preload_payload = preload.json()
            if isinstance(preload_payload, dict) and preload_payload.get("error"):
                raise RuntimeError(str(preload_payload["error"]))
            log.info(
                "Ollama model prepared request_id=%s model=%s connect_timeout=%ds "
                "read_timeout=%ds elapsed_seconds=%.3f",
                request_id, model, PREPARATION_CONNECT_TIMEOUT, PREPARATION_READ_TIMEOUT,
                time.monotonic() - preload_started,
            )
        except Exception as exc:
            log.warning(
                "Ollama model preparation failed request_id=%s model=%s exception_type=%s "
                "connect_timeout=%ds read_timeout=%ds elapsed_seconds=%.3f error=%s",
                request_id, model, type(exc).__name__, PREPARATION_CONNECT_TIMEOUT,
                PREPARATION_READ_TIMEOUT, time.monotonic() - preload_started, exc,
            )
            return RuntimeContext(context_length=FALLBACK_CONTEXT_LENGTH, source="fallback")

        try:
            response = requests.get(f"{self.base_url}/api/ps", timeout=min(self.timeout, 5))
            response.raise_for_status()
            payload = response.json()
            models = payload.get("models", []) if isinstance(payload, dict) else []
            match = next(
                (
                    item for item in models
                    if isinstance(item, dict) and (item.get("name") == model or item.get("model") == model)
                ),
                None,
            )
            context_length = match.get("context_length") if match else None
            if not self._is_valid_context_length(context_length):
                reason = "active model absent" if match is None else "missing or invalid context_length"
                log.warning(
                    "Ollama runtime context unavailable request_id=%s model=%s reason=%s context_length=%r",
                    request_id, model, reason, context_length,
                )
                return RuntimeContext(context_length=FALLBACK_CONTEXT_LENGTH, source="fallback")

            context_length = int(context_length)
            self._runtime_context_cache[cache_key] = context_length
            log.info(
                "Ollama runtime context discovered request_id=%s model=%s context_length=%d",
                request_id, model, context_length,
            )
            return RuntimeContext(context_length=context_length, source="runtime")
        except Exception as exc:
            log.warning(
                "Ollama runtime context discovery failed request_id=%s model=%s exception_type=%s error=%s",
                request_id, model, type(exc).__name__, exc,
            )
            return RuntimeContext(context_length=FALLBACK_CONTEXT_LENGTH, source="fallback")

    def get_runtime_context(self, *, model: str, options: Optional[Dict] = None) -> RuntimeContext:
        """Expose only known runtime state; callers needing a refresh must prepare off the GUI thread."""
        cached = self._runtime_context_cache.get(self._runtime_context_cache_key(model, options or {}))
        if cached is not None:
            return RuntimeContext(context_length=cached, source="cache")
        return RuntimeContext(context_length=FALLBACK_CONTEXT_LENGTH, source="fallback")

    def invalidate_runtime_context(self, *, model: Optional[str] = None) -> None:
        """Forget confirmed values after transport failure or a changed active runtime."""
        if model is None:
            self._runtime_context_cache.clear()
            return
        for key in list(self._runtime_context_cache):
            if key[1] == model:
                del self._runtime_context_cache[key]

    def _runtime_context_cache_key(
        self, model: str, options: Dict,
    ) -> Tuple[str, str, Tuple[Tuple[str, str], ...]]:
        context_options = tuple(sorted(
            (str(key), repr(value))
            for key, value in options.items()
            if key in CONTEXT_OPTION_KEYS
        ))
        return self.base_url, model, context_options

    @staticmethod
    def _is_valid_context_length(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0
