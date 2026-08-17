# hamchat/infra/llm/ollama_client.py
from __future__ import annotations
import json
import logging
import time
import uuid
import requests
from dataclasses import dataclass
from typing import Callable, Dict, Iterator, List, Optional, Tuple
from .base import ModelClient, ChatMessage, StreamEvent
from .ollama_planner import RequestTooLargeError, plan_ollama_request

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


@dataclass(frozen=True)
class UnloadResult:
    """Outcome of a best-effort unload of currently running Ollama models."""

    loaded_count: int
    unloaded: tuple[str, ...]
    failed: tuple[str, ...]
    skipped: tuple[str, ...]
    remaining: Optional[tuple[str, ...]]
    error: Optional[str] = None


class OllamaClient(ModelClient):
    supports_final_message_callback = True
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
        thinking_mode: Optional[str] = None,
        requested_thinking_mode: Optional[str] = None,
        _thinking_retry_attempt: int = 0,
        _final_messages_callback_called: bool = False,
        final_messages_callback=None,
    ) -> Iterator[StreamEvent]:
        request_id = request_id or uuid.uuid4().hex[:8]
        effective_options = dict(options or {})
        resolved_context = prepared_context or self.prepare_runtime_context(
            model=model, options=effective_options, request_id=request_id,
        )

        try:
            plan = plan_ollama_request(
                messages=messages, options=effective_options,
                context_length=resolved_context.context_length,
            )
        except RequestTooLargeError as exc:
            log.warning(
                "Ollama request plan rejected request_id=%s model=%s context_length=%d source=%s error=%s",
                request_id, model, resolved_context.context_length, resolved_context.source, exc,
            )
            yield StreamEvent(type="error", error=str(exc))
            return

        effective_options = plan.options
        messages = plan.messages
        if final_messages_callback is not None and not _final_messages_callback_called:
            final_messages_callback(messages)
        log.info(
            "Ollama request plan request_id=%s model=%s context_length=%d context_source=%s "
            "original_message_count=%d final_message_count=%d original_input_tokens=%d "
            "final_input_tokens=%d template_reserve=%d reasoning_reserve=%d "
            "visible_response_reserve=%d num_predict=%d omitted_turn_count=%d "
            "omitted_message_count=%d outcome=%s",
            request_id, model, resolved_context.context_length, resolved_context.source,
            plan.original_message_count, len(messages), plan.original_input_tokens,
            plan.final_input_tokens, plan.template_reserve, plan.reasoning_reserve,
            plan.visible_response_reserve, plan.num_predict, plan.omitted_turn_count,
            plan.omitted_message_count, plan.outcome,
        )

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
        allocation_mode = {None: "auto", 4096: "low", 8192: "mid", 16384: "high"}.get(
            requested_num_ctx, "custom",
        )
        log.info(
            "Ollama request request_id=%s model=%s options=%s message_count=%d "
            "text_char_count=%d image_count=%d approx_input_token_count=%d "
            "allocation_mode=%s requested_num_ctx=%r effective_context_length=%d effective_context_source=%s",
            request_id, model, effective_options, len(wire), text_char_count,
            image_count, approx_input_token_count, allocation_mode,
            requested_num_ctx,
            resolved_context.context_length, resolved_context.source,
        )

        think_value = self._thinking_payload_value(thinking_mode)
        log.info(
            "Ollama thinking request_id=%s model=%s requested_thinking_mode=%s effective_thinking_mode=%s",
            request_id, model, requested_thinking_mode or thinking_mode, thinking_mode,
        )

        payload = {
            "model": model,
            "messages": wire,
            "stream": True,
            "options": effective_options,
        }
        if think_value is not None:
            payload["think"] = think_value
        url = f"{self.base_url}/api/chat"

        line_number = 0
        thinking_chunk_count = thinking_char_count = 0
        visible_chunk_count = visible_char_count = 0
        malformed_line_count = 0
        stream_compromised = False
        terminal_received = False
        retry_with_low = False
        try:
            with requests.post(url, json=payload, stream=True, timeout=self.timeout) as r:
                status_code = getattr(r, "status_code", None)
                if isinstance(status_code, int) and not 200 <= status_code < 300:
                    error_text = self._response_error_text(r)
                    log.warning(
                        "Ollama HTTP error request_id=%s model=%s status=%s error=%s",
                        request_id, model, status_code, error_text,
                    )
                    if (
                        think_value is False
                        and not _thinking_retry_attempt
                        and self._is_think_false_rejection(error_text)
                    ):
                        log.warning(
                            "Ollama thinking disable rejected request_id=%s model=%s; retrying once with low",
                            request_id, model,
                        )
                        yield StreamEvent(type="thinking_forced_low", text="low")
                        yield from self.stream_chat(
                            model=model, messages=messages, options=effective_options,
                            prepared_context=resolved_context, request_id=request_id,
                            thinking_mode="low", requested_thinking_mode=requested_thinking_mode or thinking_mode,
                            _thinking_retry_attempt=1,
                            _final_messages_callback_called=True,
                            final_messages_callback=final_messages_callback,
                        )
                        return
                    if (
                        think_value is not None
                        and not _thinking_retry_attempt
                        and self._is_thinking_unsupported(error_text)
                    ):
                        log.warning(
                            "Ollama thinking unsupported request_id=%s model=%s; retrying once without think",
                            request_id, model,
                        )
                        yield StreamEvent(type="thinking_unsupported", text=thinking_mode or "")
                        yield from self.stream_chat(
                            model=model, messages=messages, options=effective_options,
                            prepared_context=resolved_context, request_id=request_id,
                            thinking_mode=None,
                            requested_thinking_mode=requested_thinking_mode or thinking_mode,
                            _thinking_retry_attempt=1,
                            _final_messages_callback_called=True,
                            final_messages_callback=final_messages_callback,
                        )
                        return
                    self.invalidate_runtime_context()
                    if thinking_mode is not None and self._is_thinking_rejection(error_text):
                        yield StreamEvent(type="thinking_rejected", text=thinking_mode)
                    yield StreamEvent(type="error", error=f"Ollama request failed [{request_id}]: {error_text}")
                    return
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
                        output_started = bool(thinking_chunk_count or visible_chunk_count)
                        if (
                            think_value is False
                            and not output_started
                            and not _thinking_retry_attempt
                            and self._is_think_false_rejection(error)
                        ):
                            log.warning(
                                "Ollama thinking disable rejected request_id=%s model=%s; retrying once with low",
                                request_id, model,
                            )
                            retry_with_low = True
                            break
                        if (
                            think_value is not None
                            and not output_started
                            and not _thinking_retry_attempt
                            and self._is_thinking_unsupported(error)
                        ):
                            log.warning(
                                "Ollama thinking unsupported request_id=%s model=%s; retrying once without think",
                                request_id, model,
                            )
                            yield StreamEvent(type="thinking_unsupported", text=thinking_mode or "")
                            yield from self.stream_chat(
                                model=model, messages=messages, options=effective_options,
                                prepared_context=resolved_context, request_id=request_id,
                                thinking_mode=None,
                                requested_thinking_mode=requested_thinking_mode or thinking_mode,
                                _thinking_retry_attempt=1,
                                _final_messages_callback_called=True,
                                final_messages_callback=final_messages_callback,
                            )
                            return
                        if thinking_mode is not None and not output_started and self._is_thinking_rejection(error):
                            yield StreamEvent(type="thinking_rejected", text=thinking_mode)
                        yield StreamEvent(type="error", error=f"Ollama error [{request_id}]: {error}")
                        return
                    message_obj = obj.get("message")
                    msg = message_obj if isinstance(message_obj, dict) else {}
                    thinking = msg.get("thinking") or ""
                    if thinking:
                        thinking_chunk_count += 1
                        thinking_char_count += len(thinking)
                        yield StreamEvent(type="thinking", text=thinking)
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
                if retry_with_low:
                    pass
                elif not terminal_received:
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
            if retry_with_low:
                yield StreamEvent(type="thinking_forced_low", text="low")
                yield from self.stream_chat(
                    model=model, messages=messages, options=effective_options,
                    prepared_context=resolved_context, request_id=request_id,
                    thinking_mode="low", requested_thinking_mode=requested_thinking_mode or thinking_mode,
                    _thinking_retry_attempt=1,
                    _final_messages_callback_called=True,
                    final_messages_callback=final_messages_callback,
                )
                return
        except Exception as e:
            response = getattr(e, "response", None)
            error_text = self._response_error_text(response) if response is not None else str(e)
            if response is not None:
                log.warning(
                    "Ollama HTTP exception request_id=%s model=%s status=%s error=%s",
                    request_id, model, getattr(response, "status_code", None), error_text,
                )
            output_started = bool(thinking_chunk_count or visible_chunk_count)
            if (
                think_value is False
                and not output_started
                and not _thinking_retry_attempt
                and self._is_think_false_rejection(error_text)
            ):
                log.warning(
                    "Ollama thinking disable HTTP rejection request_id=%s model=%s; retrying once with low",
                    request_id, model,
                )
                yield StreamEvent(type="thinking_forced_low", text="low")
                yield from self.stream_chat(
                    model=model, messages=messages, options=effective_options,
                    prepared_context=resolved_context, request_id=request_id,
                    thinking_mode="low", requested_thinking_mode=requested_thinking_mode or thinking_mode,
                    _thinking_retry_attempt=1,
                    _final_messages_callback_called=True,
                    final_messages_callback=final_messages_callback,
                )
                return
            if (
                think_value is not None
                and not output_started
                and not _thinking_retry_attempt
                and self._is_thinking_unsupported(error_text)
            ):
                log.warning(
                    "Ollama thinking unsupported HTTP rejection request_id=%s model=%s; retrying once without think",
                    request_id, model,
                )
                yield StreamEvent(type="thinking_unsupported", text=thinking_mode or "")
                yield from self.stream_chat(
                    model=model, messages=messages, options=effective_options,
                    prepared_context=resolved_context, request_id=request_id,
                    thinking_mode=None,
                    requested_thinking_mode=requested_thinking_mode or thinking_mode,
                    _thinking_retry_attempt=1,
                    _final_messages_callback_called=True,
                    final_messages_callback=final_messages_callback,
                )
                return
            self.invalidate_runtime_context()
            log.warning(
                "Ollama request failed request_id=%s exception_type=%s error=%s",
                request_id, type(e).__name__, error_text,
            )
            if thinking_mode is not None and not output_started and self._is_thinking_rejection(error_text):
                yield StreamEvent(type="thinking_rejected", text=thinking_mode)
            yield StreamEvent(type="error", error=f"Ollama request failed [{request_id}]: {error_text}")

    @staticmethod
    def _thinking_payload_value(mode: Optional[str]):
        return {"off": False, "low": "low", "high": "high"}.get(mode)

    @staticmethod
    def _is_think_false_rejection(error: str) -> bool:
        normalized = (error or "").lower()
        return "think" in normalized and any(token in normalized for token in ("false", "disable", "unsupported"))

    @staticmethod
    def _is_thinking_rejection(error: str) -> bool:
        return "think" in (error or "").lower()

    @staticmethod
    def _is_thinking_unsupported(error: str) -> bool:
        """Match Ollama's explicit capability diagnostic, not generic errors."""
        return "does not support thinking" in (error or "").lower()

    @staticmethod
    def _response_error_text(response) -> str:
        """Prefer Ollama's structured error, even when its response is falsey."""
        try:
            payload = response.json()
            if isinstance(payload, dict) and payload.get("error") is not None:
                return str(payload["error"])
        except Exception:
            pass
        try:
            text = response.text
            if text:
                return str(text)
        except Exception:
            pass
        try:
            response.raise_for_status()
        except Exception as exc:
            return str(exc)
        return "HTTP request failed without an error body"

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
                json={"model": model, "prompt": "", "stream": False, "options": effective_options},
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

    def unload_all_models(
        self,
        *,
        capabilities_for: Optional[Callable[[str], Optional[List[str]]]] = None,
        timeout: tuple[float, float] = (1, 5),
    ) -> UnloadResult:
        """Unload known running models without guessing their Ollama endpoint."""
        try:
            running = self._running_models(timeout)
        except Exception as exc:
            return UnloadResult(0, (), (), (), None, str(exc))
        if not running:
            return UnloadResult(0, (), (), (), ())

        unloaded: list[str] = []
        failed: list[str] = []
        skipped: list[str] = []
        for name in running:
            reported = capabilities_for(name) if capabilities_for else None
            capabilities = self._normalize_capabilities(reported)
            if capabilities is None:
                capabilities = self._show_capabilities(name, timeout)

            if capabilities is None:
                skipped.append(name)
                continue
            if "completion" in capabilities:
                endpoint, payload = "/api/generate", {
                    "model": name, "prompt": "", "stream": False, "keep_alive": 0,
                }
            elif "embedding" in capabilities:
                endpoint, payload = "/api/embed", {
                    "model": name, "input": "", "keep_alive": 0,
                }
            else:
                skipped.append(name)
                continue
            try:
                response = requests.post(f"{self.base_url}{endpoint}", json=payload, timeout=timeout)
                response.raise_for_status()
                unloaded.append(name)
                self.invalidate_runtime_context(model=name)
            except Exception as exc:
                log.warning("Ollama unload failed model=%s endpoint=%s error=%s", name, endpoint, exc)
                failed.append(name)

        try:
            remaining = tuple(self._running_models(timeout))
        except Exception as exc:
            return UnloadResult(len(running), tuple(unloaded), tuple(failed), tuple(skipped), None, str(exc))
        still_loaded = set(remaining)
        verified_unloaded = [name for name in unloaded if name not in still_loaded]
        for name in unloaded:
            if name in still_loaded and name not in failed:
                failed.append(name)
        return UnloadResult(len(running), tuple(verified_unloaded), tuple(failed), tuple(skipped), remaining)

    def unload_model_if_running(
        self,
        model: str,
        *,
        capabilities: Optional[List[str]] = None,
        timeout: tuple[float, float] = (0.25, 1.0),
    ) -> bool:
        """Best-effort unload of one exact, already-running model only."""
        if not model:
            return False
        try:
            response = requests.get(f"{self.base_url}/api/ps", timeout=timeout)
            response.raise_for_status()
            payload = response.json()
            running = payload.get("models", []) if isinstance(payload, dict) else []
        except Exception as exc:
            log.warning("Ollama shutdown unload ps failed model=%s error=%s", model, exc)
            return False
        if not any(
            isinstance(item, dict) and (item.get("name") == model or item.get("model") == model)
            for item in running
        ):
            return False

        resolved = self._normalize_capabilities(capabilities)
        if resolved is None:
            resolved = self._show_capabilities(model, timeout)
        if resolved is None:
            log.warning("Ollama shutdown unload skipped model=%s reason=unknown_capabilities", model)
            return False
        if "completion" in resolved:
            endpoint, payload = "/api/generate", {
                "model": model, "prompt": "", "stream": False, "keep_alive": 0,
            }
        else:
            log.warning("Ollama shutdown unload skipped model=%s reason=unsupported_capabilities", model)
            return False
        try:
            response = requests.post(f"{self.base_url}{endpoint}", json=payload, timeout=timeout)
            response.raise_for_status()
            self.invalidate_runtime_context(model=model)
            return True
        except Exception as exc:
            log.warning("Ollama shutdown unload failed model=%s endpoint=%s error=%s", model, endpoint, exc)
            return False

    def _running_models(self, timeout: tuple[float, float]) -> list[str]:
        response = requests.get(f"{self.base_url}/api/ps", timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        models = payload.get("models", []) if isinstance(payload, dict) else []
        names: list[str] = []
        for item in models:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("model")
            if isinstance(name, str) and name and name not in names:
                names.append(name)
        return names

    def _show_capabilities(self, model: str, timeout: tuple[float, float]) -> Optional[set[str]]:
        try:
            response = requests.post(
                f"{self.base_url}/api/show", json={"name": model}, timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            log.warning("Ollama capability lookup failed model=%s error=%s", model, exc)
            return None
        return self._normalize_capabilities(payload.get("capabilities") if isinstance(payload, dict) else None)

    @staticmethod
    def _normalize_capabilities(value: object) -> Optional[set[str]]:
        if not isinstance(value, list):
            return None
        return {
            capability.strip().lower()
            for capability in value
            if isinstance(capability, str) and capability.strip()
        }

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
