# hamchat/ui/chat_controller.py
from __future__ import annotations
import logging
import math
import re
from dataclasses import dataclass
from typing import Callable, Optional, List, Dict
from PyQt6.QtCore import QObject, Qt, pyqtSignal

from hamchat.infra.llm.thread_broker import ThreadBroker
from hamchat.infra.llm.base import ChatMessage
from hamchat.infra.llm.backend_adapter import make_stream_func_from_client
from hamchat import db_ops as dbo  # persistence API (create_conversation, add_message)
from hamchat.core.session import SessionManager
from hamchat.media_helper import process_images
from hamchat.infra.llm.base import ModelClient  # if you want to type-hint, optional
from hamchat.memory_embeddings import MemoryEmbeddingService, OllamaEmbeddingProvider

log = logging.getLogger("ui.chat_controller")

DEFAULT_GENERATION_OPTIONS = {"temperature": 0.7}
_TEMPERATURE_RANGE = (0.0, 2.0)
_TOP_P_RANGE = (0.0, 1.0)


@dataclass
class HistoryEntry:
    db_id: Optional[int]   # database messages.id, or None for unsaved/ephemeral
    msg: ChatMessage


class ChatController(QObject):
    """
    Glue between the chat display widget and the LLM backend.

    Responsibilities:
    - Keep an in-memory rolling history of the conversation.
    - Start / stop streamed generations via ThreadBroker.
    - Forward tokens and final messages to the chat display.
    - Allow changing the active model at runtime (set_model_name).
    """

    # Fired when we lazily create a saved_conversations row for a user chat
    conversation_started = pyqtSignal(int)  # conversation_id
    # Fired when we programmatically create a forked conversation and want the UI to open it
    forked_conversation = pyqtSignal(int)   # conversation_id
    ham_mem_status = pyqtSignal(str)

    def __init__(        self,
        chat_display,
        model_client,
        *,
        model_name: str,
        parent: Optional[QObject] = None,
        db=None,
        data_dir=None,
        session: Optional[SessionManager] = None,
        local_command_handler: Optional[Callable[[str], bool]] = None,
        thinking_panel=None,
    ):
        super().__init__(parent)
        self.chat = chat_display
        self.broker = ThreadBroker(self)

        # Keep a handle to the client + current model so we can reconfigure later
        self._model_client = model_client
        self._model_name = model_name
        self._data_dir = data_dir

        # ---- In-memory session history ----
        self._history: List[HistoryEntry] = []
        self._assistant_buf: List[str] = []
        self._max_turns: int = 512   # rolling window; adjust as needed
        # We should set the max turns in the session, load it from app.json, or infer it from spec report maybe

        # ---- Persistence context (optional; enabled only for role='user') ----
        self._db = db
        self._session = session
        self._local_command_handler = local_command_handler
        self._thinking_panel = thinking_panel
        self._thinking_mode = "medium"
        self._thinking_notice_shown = False
        # Only clear notices that this controller created for a capability
        # state; forced-low and rejection notices have separate lifecycles.
        self._thinking_capability_notice_model: Optional[str] = None
        self._use_ham_mem = True
        self._conv_id: Optional[int] = None  # lazily created on first user msg
        memory_cfg = getattr(session, "settings", None).get("ham_mem", {}) if session is not None else {}
        embedding_model = (memory_cfg.get("embedding_model") or "nomic-embed-text").strip() if isinstance(memory_cfg, dict) else "nomic-embed-text"
        if not embedding_model:
            embedding_model = "nomic-embed-text"
        self._memory_provider = OllamaEmbeddingProvider(model_id=embedding_model)
        self._memory_service = MemoryEmbeddingService(db, self._memory_provider) if db is not None else None

        # Build the initial streaming function for the starting model
        self._configure_stream()

        # UI → controller
        self.chat.sig_send_text.connect(self._on_user_text, Qt.ConnectionType.QueuedConnection)
        self.chat.sig_stop_requested.connect(self._on_stop, Qt.ConnectionType.QueuedConnection)
        if self._thinking_panel is not None:
            self._thinking_panel.thinkingModeChanged.connect(
                self._on_thinking_mode_changed, Qt.ConnectionType.QueuedConnection,
            )
            self._thinking_panel.hamMemChanged.connect(self._on_use_ham_mem_changed, Qt.ConnectionType.QueuedConnection)

        # Broker → UI
        self.broker.job_token.connect(self._on_job_token, Qt.ConnectionType.QueuedConnection)
        self.broker.job_thinking.connect(self._on_job_thinking, Qt.ConnectionType.QueuedConnection)
        self.broker.job_notice.connect(self._on_job_notice, Qt.ConnectionType.QueuedConnection)
        self.broker.job_memory_snapshot.connect(self._on_job_memory_snapshot, Qt.ConnectionType.QueuedConnection)
        self.broker.job_finished.connect(self._on_job_finished, Qt.ConnectionType.QueuedConnection)
        self.broker.job_error.connect(self._on_job_error, Qt.ConnectionType.QueuedConnection)

        self._active_row: Optional[int] = None
        self._active_ticket: int = -1
        # This is invalidated immediately on cancel, before the worker has
        # necessarily delivered all queued cross-thread signals.
        self._thinking_ticket: int = -1
        self._thinking_generation_state: dict[int, dict] = {}
        self._refresh_thinking_controls()
        self._refresh_ham_mem_control()

    def _clear_thinking(self) -> None:
        if self._thinking_panel is not None:
            self._thinking_panel.clear_thinking()

    def _begin_thinking_generation(self) -> None:
        self._thinking_ticket = -1
        self._clear_thinking()
        self._clear_memory_snapshot()
        log.debug("Transient thinking cleared for new generation")

    def _activate_thinking_for_ticket(self, ticket: int) -> None:
        self._thinking_ticket = ticket
        self._refresh_thinking_controls()
        self._refresh_ham_mem_control()

    def clear_transient_thinking(self) -> None:
        """Forget non-persistent reasoning when the active session is unloaded."""
        self._thinking_ticket = -1
        self._clear_thinking()
        self._clear_memory_snapshot()

    def reset_chat_memory_preferences(self) -> None:
        """Reset the unsaved/new-chat HamMem choice when a session is unloaded."""
        self._use_ham_mem = True
        self._refresh_ham_mem_control()

    def _clear_memory_snapshot(self) -> None:
        if self._thinking_panel is not None and hasattr(self._thinking_panel, "clear_memory_snapshot"):
            self._thinking_panel.clear_memory_snapshot()

    def _refresh_ham_mem_control(self) -> None:
        if self._thinking_panel is not None:
            if hasattr(self._thinking_panel, "set_use_ham_mem"):
                self._thinking_panel.set_use_ham_mem(self._use_ham_mem, control_enabled=self._active_ticket == -1)

    def _on_use_ham_mem_changed(self, enabled: bool) -> None:
        self._use_ham_mem = bool(enabled)
        if self._conv_id and self._save_enabled():
            try:
                dbo.set_conversation_use_ham_mem(self._db, self._conv_id, self._use_ham_mem)
            except Exception:
                log.exception("Could not save conversation HamMem setting")

    def _thinking_model_info(self) -> tuple[bool, bool, bool]:
        """Return (controls_enabled, requires_thinking, support_unverified)."""
        if not hasattr(self._model_client, "prepare_runtime_context"):
            return False, False, False
        caps = self._session.get_model_capabilities(self._model_name) if self._session else {}
        if isinstance(caps.get("thinking"), bool):
            return caps["thinking"], False, False
        metadata = self._session.get_model_metadata(self._model_name) if self._session and hasattr(self._session, "get_model_metadata") else {}
        family = str((metadata or {}).get("family") or caps.get("family") or "").lower().replace("_", "-")
        model = self._model_name.lower().replace("_", "-")
        is_gpt_oss = family in {"gptoss", "gpt-oss"} or model.startswith("gpt-oss:")
        supports = is_gpt_oss or family in {"qwen3", "qwen35", "qwen3.5", "deepseek-r1"}
        if supports:
            return True, is_gpt_oss, False
        # No registry assertion is not an assertion of no support.  Let Ollama
        # decide and surface an explicit rejection if the override is invalid.
        return True, False, True

    def _refresh_thinking_controls(self) -> None:
        if self._thinking_panel is None:
            return
        supports, _, unverified = self._thinking_model_info()
        self._thinking_panel.set_thinking_mode(self._thinking_mode, enabled=supports and self._active_ticket == -1)
        self._thinking_panel.set_thinking_support_unverified(unverified)
        caps = self._session.get_model_capabilities(self._model_name) if self._session else {}
        is_ollama = hasattr(self._model_client, "prepare_runtime_context")
        if is_ollama and caps.get("thinking") is False:
            self._thinking_panel.show_thinking_notice(
                f"{self._model_name} does not support Ollama thinking controls. "
                "HamChat will omit the thinking setting for this model."
            )
            self._thinking_capability_notice_model = self._model_name
        elif getattr(self, "_thinking_capability_notice_model", None) is not None:
            # Do not erase notices produced by the forced-low/rejection paths.
            self._thinking_panel.show_thinking_notice("")
            self._thinking_capability_notice_model = None

    def _set_thinking_mode(self, mode: str, *, show_forced_notice: bool = False) -> None:
        self._thinking_mode = mode if mode in {"off", "low", "medium", "high"} else "medium"
        if self._conv_id and self._save_enabled():
            try:
                dbo.set_conversation_thinking_mode(self._db, self._conv_id, self._thinking_mode)
            except Exception:
                log.exception("Could not save conversation thinking mode")
        if self._thinking_panel is not None:
            self._refresh_thinking_controls()
            if show_forced_notice and not self._thinking_notice_shown:
                self._thinking_panel.show_thinking_notice(
                    "Thinking can’t be disabled for this model, so HamChat has set it to Low."
                )
                self._thinking_notice_shown = True

    def _on_thinking_mode_changed(self, mode: str) -> None:
        self._thinking_notice_shown = False
        self._set_thinking_mode(mode)

    def _request_thinking_modes(self) -> tuple[Optional[str], Optional[str]]:
        requested_mode = self._thinking_mode
        supports, requires_thinking, _ = self._thinking_model_info()
        if not supports:
            return None, None
        if requires_thinking and self._thinking_mode == "off":
            self._set_thinking_mode("low", show_forced_notice=True)
        return requested_mode, self._thinking_mode

    def _register_thinking_generation(
        self, ticket: int, requested_mode: Optional[str], effective_mode: Optional[str],
    ) -> None:
        """Remember the submission-time facts needed for capability learning."""
        caps = self._session.get_model_capabilities(self._model_name) if self._session else {}
        self._thinking_generation_state[ticket] = {
            "model": self._model_name,
            "unknown_at_submission": not isinstance(caps.get("thinking"), bool),
            "explicit_think_sent": effective_mode in {"off", "low", "high"},
            "unsupported": False,
            "requested_mode": requested_mode,
        }

    def _set_learned_thinking_capability(self, model: str, value: bool) -> None:
        if self._session is None or not hasattr(self._session, "set_model_capability"):
            return
        try:
            self._session.set_model_capability(model, "thinking", value)
        except Exception:
            log.exception("Could not persist thinking capability for model %s", model)

    def set_model_client(self, model_client) -> None:
        """
        Swap out the underlying LLM backend (e.g. OllamaClient vs OpenAIClient).
        Safe to call between requests; the new client will be used for the next prompt.
        """
        self._model_client = model_client
        self._configure_stream()
        self._refresh_thinking_controls()

    def get_active_runtime_context(self, options: Optional[Dict] = None):
        """Return known Ollama runtime context without doing network I/O on the GUI thread."""
        get_runtime_context = getattr(self._model_client, "get_runtime_context", None)
        if callable(get_runtime_context):
            return get_runtime_context(model=self._model_name, options=options or {})
        return None

    def on_model_context_allocation_changed(self, model_id: str, _mode: str) -> None:
        """Forget only the active runtime's confirmed context; no GUI-thread I/O."""
        if model_id != self._model_name:
            return
        invalidate_runtime_context = getattr(self._model_client, "invalidate_runtime_context", None)
        if callable(invalidate_runtime_context):
            invalidate_runtime_context(model=model_id)
        self._configure_stream()

    # ---------- Persistence helpers ----------
    def _save_enabled(self) -> bool:
        """
        Saving is enabled only when a real user (not guest/admin) is chatting.
        """
        try:
            return (
                    self._db is not None
                    and self._session is not None
                    and getattr(self._session.current, "role", "guest") == "user"
                    and self._session.current.user_id is not None
            )
        except Exception:
            return False

    def _ensure_conversation(self, title: str) -> None:
        if self._conv_id or not self._save_enabled():
            return
        # Use the first user prompt (trimmed) as the title
        safe_title = (title or "Untitled").strip()
        if len(safe_title) > 80:
            safe_title = safe_title[:80] + "…"
        try:
            uid = int(self._session.current.user_id)  # type: ignore
            self._conv_id = dbo.create_conversation(
                self._db, user_id=uid, title=safe_title, thinking_mode=self._thinking_mode, use_ham_mem=self._use_ham_mem,
            )
            # Notify listeners (e.g., MainWindow → SidePanel) that a new convo exists
            self.conversation_started.emit(int(self._conv_id))
        except Exception:
            # Do not break UX if saving fails
            self._conv_id = None

    def _persist_user_with_attachments(
        self,
        text: str,
        attachments_meta: Optional[List[Dict]] = None,
    ) -> Optional[int]:
        """
        Persist a user message with attachments and return the DB message id, or None on failure.
        """
        if not self._save_enabled():
            return None
        try:
            self._ensure_conversation(text)
            if not self._conv_id:
                return None
            uid = int(self._session.current.user_id)  # type: ignore
            mid = dbo.add_message(
                self._db,
                conversation_id=int(self._conv_id),
                sender_type="user",
                sender_id=uid,
                content=text,
                metadata={"attachments": attachments_meta} if attachments_meta else None,
            )
            return int(mid)
        except Exception:
            # do not break UX if persistence fails
            return None

    # ---- helpers for forking ----------------------------------------

    def _make_fork_title(self) -> str:
        """
        Generate a unique '... - Forked N' title based on the current conversation title.
        """
        if not self._db or not self._conv_id:
            return "Forked chat"

        cur = self._db.cursor()
        cur.execute(
            "SELECT title FROM saved_conversations WHERE id = ?",
            (int(self._conv_id),),
        )
        row = cur.fetchone()
        base = (row[0] if row and row[0] else "Untitled").strip()

        # Extract root + existing fork number, if any.
        m = re.match(r"^(.*?)(?:\s-\sForked\s(\d+))?$", base)
        if m:
            root = (m.group(1) or "").strip()
            num = m.group(2)
            n = int(num) + 1 if num is not None else 1
        else:
            root = base
            n = 1

        return f"{root} - Forked {n}"

    def _clone_message_to_conversation(self, new_conv_id: int, row: Dict) -> None:
        """
        Clone a single message row (from list_messages) into a new conversation.
        """
        if not self._db:
            return

        sender_type = row.get("sender_type", "assistant")
        sender_id = row.get("sender_id")
        content = row.get("content") or ""
        metadata = row.get("metadata") or None

        try:
            dbo.add_message(
                self._db,
                conversation_id=int(new_conv_id),
                sender_type=sender_type,
                sender_id=sender_id,
                content=content,
                metadata=metadata,
            )
        except Exception:
            # Don't let a single bad row kill the fork.
            pass

    # ---------- Configuration ----------

    def _get_active_profile_id(self) -> Optional[int]:
        """
        Best-effort lookup of the currently active AI profile id.

        Returns:
            - int id for a real profile
            - 0 or None for the synthetic 'Default' / no persona
        """
        try:
            if self._session is None:
                return None
            if not hasattr(self._session, "get_profile_id"):
                return None
            pid = self._session.get_profile_id()
            user_id = getattr(getattr(self._session, "current", None), "user_id", None)
            log.debug(
                "Profile request context: active_profile_id=%r (%s), user_id=%r (%s)",
                pid,
                type(pid).__name__,
                user_id,
                type(user_id).__name__,
            )
            try:
                return int(pid) if pid is not None else None
            except Exception:
                return None
        except Exception:
            return None

    def _get_active_profile_row(self) -> Optional[dict]:
        """
        Fetch the full ai_profiles row for the currently active profile,
        or None if default / missing / DB unavailable.
        """
        if self._db is None or self._session is None:
            return None

        pid = self._get_active_profile_id()
        if pid in (None, 0):
            return None

        try:
            profile = dbo.get_ai_profile(self._db, int(pid))
        except Exception:
            log.debug("Active profile lookup failed for id=%r", pid, exc_info=True)
            return None

        if profile is None:
            log.debug("Active profile lookup: id=%r found=False", pid)
            return None

        values = {key: type(value).__name__ for key, value in profile.items()}
        raw_prompt = profile.get("system_prompt")
        prompt_length = len(raw_prompt) if isinstance(raw_prompt, str) else None
        log.debug(
            "Active profile lookup: id=%r found=True keys=%s value_types=%s system_prompt_length=%r",
            pid,
            sorted(profile.keys()),
            values,
            prompt_length,
        )
        return profile

    def system_injection_if_any(self) -> Optional[ChatMessage]:
        """
        Build a system-level 'rule injection' message for the active AI profile, if it has
        a non-empty system_prompt. Returns None if there's nothing to inject.
        """
        profile = self._get_active_profile_row()
        return self._system_injection_for_profile(profile)

    def _system_injection_for_profile(self, profile: Optional[dict]) -> Optional[ChatMessage]:
        if not profile:
            return None

        raw_prompt = (profile.get("system_prompt") or "").strip()
        if not raw_prompt:
            log.debug("Active profile system prompt resolved: present=False length=0")
            return None

        log.debug("Active profile system prompt resolved: present=True length=%d", len(raw_prompt))

        meta = {
            "kind": "rule_injection",
            "hidden": True,
            "profile_id": profile.get("id"),
        }

        return ChatMessage(role="system", content=raw_prompt, metadata=meta)

    def _profile_float_option(
        self,
        profile: dict,
        field: str,
        minimum: float,
        maximum: float,
    ) -> Optional[float]:
        raw_value = profile.get(field)
        if raw_value is None:
            return None
        try:
            if isinstance(raw_value, bool):
                raise ValueError("boolean is not a numeric generation option")
            value = float(raw_value)
            if not math.isfinite(value):
                raise ValueError("value is not finite")
        except (TypeError, ValueError):
            log.warning(
                "Ignoring malformed profile generation option: profile_id=%r field=%s type=%s",
                profile.get("id"),
                field,
                type(raw_value).__name__,
            )
            return None

        clamped = max(minimum, min(maximum, value))
        if clamped != value:
            log.warning(
                "Clamped profile generation option: profile_id=%r field=%s value=%s range=[%s, %s]",
                profile.get("id"),
                field,
                value,
                minimum,
                maximum,
            )
        return clamped

    def _resolve_active_profile_request(self) -> tuple[Optional[ChatMessage], Dict]:
        """Resolve profile prompt and generation options on the GUI thread."""
        profile = self._get_active_profile_row()
        options = dict(DEFAULT_GENERATION_OPTIONS)
        if not profile:
            log.debug("Profile generation options resolved: %s", options)
            return None, options

        temperature = self._profile_float_option(
            profile, "temperature", *_TEMPERATURE_RANGE
        )
        if temperature is not None:
            options["temperature"] = temperature

        top_p = self._profile_float_option(profile, "top_p", *_TOP_P_RANGE)
        if top_p is not None:
            options["top_p"] = top_p

        log.debug(
            "Profile generation options resolved: profile_id=%r options=%s",
            profile.get("id"),
            options,
        )
        return self._system_injection_for_profile(profile), options

    def _memory_snapshot(self, enabled: bool = True):
        if not enabled or not self._memory_service or not self._session or not self._session.current.user_id:
            return ([], [])
        try:
            return self._memory_service.snapshot_context(user_id=int(self._session.current.user_id), role=self._session.current.role, conversation_id=self._conv_id, profile_id=self._get_active_profile_id())
        except Exception:
            log.debug("HamMem snapshot unavailable", exc_info=True); return ([], [])

    def _report_ham_mem(self, status: str) -> None:
        """Non-persisted diagnostic hook for status bars/developer tooling."""
        log.debug("%s", status)
        self.ham_mem_status.emit(status)

    def _memory_prefix(self, prompt: str, snapshot, injection: Optional[ChatMessage]) -> List[ChatMessage]:
        if snapshot == ([], []):
            return [injection] if injection is not None else []
        memory_context, memory_status, details = MemoryEmbeddingService.format_context(
            prompt, self._memory_provider, snapshot, return_details=True,
        )
        self._report_ham_mem(memory_status)
        prefix: List[ChatMessage] = []
        role = str(getattr(getattr(self._session, "current", None), "role", "")).lower()
        if memory_context and memory_context.startswith("[HamMem administrative"):
            global_part, _, relevant_part = memory_context.partition("\n\n[HamMem relevant memory]")
            managed = details["managed"]
            marker = {"ham_mem_view": global_part if role == "admin" else f"{len(managed)} managed memor{'y' if len(managed) == 1 else 'ies'} included"}
            prefix.append(ChatMessage(role="system", content=global_part, metadata=marker))
        else:
            relevant_part = memory_context or ""
        if injection is not None:
            prefix.append(injection)
        if relevant_part:
            content = "[HamMem relevant memory]" + relevant_part if not relevant_part.startswith("[HamMem") else relevant_part
            prefix.append(ChatMessage(role="system", content=content, metadata={"ham_mem_view": content}))
        return prefix

    @staticmethod
    def _final_memory_snapshot(messages: List[ChatMessage]) -> Optional[str]:
        parts = [str(message.metadata["ham_mem_view"]) for message in messages if message.metadata and message.metadata.get("ham_mem_view")]
        return "\n\n".join(parts) if parts else None

    def _make_text_stream(self, injection: Optional[ChatMessage], options: Optional[Dict] = None, memory_snapshot=None):
        """Create a text-only stream using a prompt resolved on the GUI thread."""
        request_options = dict(DEFAULT_GENERATION_OPTIONS if options is None else options)
        self._apply_model_context_allocation(request_options)
        requested_thinking_mode, thinking_mode = self._request_thinking_modes()

        def _build_messages(prompt: str) -> List[ChatMessage]:
            hist: List[ChatMessage] = []

            hist.extend(self._memory_prefix(prompt, memory_snapshot or ([], []), injection))

            for entry in self._history[-self._max_turns * 2:]:
                m = entry.msg
                has_attachments = bool(m.metadata and m.metadata.get("attachments"))
                has_text = bool(m.content)

                # Only include a text message if there is actually text
                if has_text:
                    hist.append(ChatMessage(role=m.role, content=m.content))

                # For any message with attachments, add a stub
                if has_attachments:
                    stub = self._attachment_stub_for_model(m.metadata["attachments"])
                    if stub:
                        # Keep the descriptive history stub coupled to its text
                        # user turn so outbound context planning cannot trim one
                        # without the other. Image-only turns remain a standalone
                        # user message because there is no text parent to retain.
                        stub_metadata = {"attachment_stub_parent": True} if has_text else None
                        hist.append(ChatMessage(role="user", content=stub, metadata=stub_metadata))

            return hist

        def _build_options() -> dict:
            return dict(request_options)

        stream = make_stream_func_from_client(
            self._model_client,
            model=self._model_name,
            build_messages=_build_messages,
            build_options=_build_options,
            build_thinking=lambda: thinking_mode,
            build_requested_thinking=lambda: requested_thinking_mode,
            on_final_messages=self._final_memory_snapshot,
        )
        setattr(stream, "_thinking_generation", (requested_thinking_mode, thinking_mode))
        return stream

    def _apply_model_context_allocation(self, options: Dict) -> None:
        """Apply the saved Ollama tier before background preparation begins."""
        if self._session is None or not hasattr(self._session, "get_model_context_num_ctx"):
            return
        if not hasattr(self._model_client, "prepare_runtime_context"):
            return
        num_ctx = self._session.get_model_context_num_ctx(self._model_name)
        if num_ctx is None:
            options.pop("num_ctx", None)
        else:
            options["num_ctx"] = num_ctx

    def _configure_stream(self) -> None:
        """
        (Re)build the stream_func with the current model.
        Called on init and whenever set_model_name is used.
        """

        self.stream_func = self._make_text_stream(None, DEFAULT_GENERATION_OPTIONS)

    def set_model_name(self, model_name: str) -> None:
        """
        Update the model used for future generations.

        Safe to call between requests; it won't interrupt an active stream,
        but the new model will be used for the *next* prompt.
        """
        if model_name == self._model_name:
            return
        invalidate_runtime_context = getattr(self._model_client, "invalidate_runtime_context", None)
        if callable(invalidate_runtime_context):
            invalidate_runtime_context()
        self._model_name = model_name
        self._configure_stream()
        self._refresh_thinking_controls()

    # ---------- Slots ----------

    def _on_user_text(self, text: str):
        """
        Handle a plain-text user turn.

        - Optionally grabs pending attachments from the UI (for metadata only).
        - Persists to DB first (if enabled) and captures the message row id.
        - Appends a ChatMessage with metadata for attachments.
        """
        if self._local_command_handler:
            try:
                if self._local_command_handler(text):
                    return
            except Exception:
                log.exception("Local command handling failed")

        # Reset assistant buffer for this turn
        self._assistant_buf = []
        self._begin_thinking_generation()

        # Optional metadata: include pending attachments if any
        attachments_meta: List[Dict] = []
        if hasattr(self.chat, "get_pending_attachments"):
            try:
                attachments_meta = self.chat.get_pending_attachments() or []
            except Exception:
                attachments_meta = []

        base_meta: Optional[Dict] = {"attachments": attachments_meta} if attachments_meta else None

        # --- Persistence: create conversation (first turn) + save user message
        msg_db_id: Optional[int] = None
        if self._save_enabled():
            try:
                self._ensure_conversation(text)
                if self._conv_id:
                    msg_db_id = dbo.add_message(
                        self._db,
                        conversation_id=int(self._conv_id),
                        sender_type="user",
                        sender_id=int(self._session.current.user_id),  # type: ignore
                        content=text,
                        metadata=base_meta,
                    )
                    if msg_db_id is not None:
                        msg_db_id = int(msg_db_id)
            except Exception:
                # ignore persistence errors; keep chat flowing
                msg_db_id = None

        # Build metadata for in-memory history (attachments only)
        msg_metadata: Optional[Dict] = dict(base_meta) if base_meta else None

        # Record user turn into the rolling history
        msg = ChatMessage(
            role="user",
            content=text,
            metadata=msg_metadata or None,   # attachments only
        )
        self._history.append(
            HistoryEntry(
                db_id=msg_db_id,
                msg=msg,
            )
        )

        # Prepare UI row and kick off background job
        self._active_row = self.chat.begin_assistant_stream()
        self.chat.set_streaming(True)
        injection, options = self._resolve_active_profile_request()
        stream_func = self._make_text_stream(injection, options, self._memory_snapshot(self._use_ham_mem))
        self._active_ticket = self.broker.submit(stream_func, text)
        requested_mode, effective_mode = getattr(stream_func, "_thinking_generation", (None, None))
        self._register_thinking_generation(self._active_ticket, requested_mode, effective_mode)
        self._activate_thinking_for_ticket(self._active_ticket)

    def send_user_with_media(self, text: str, llm_parts: List[Dict], attachments_meta: Optional[List[Dict]] = None):
        """
        Send a user turn that includes vision parts (base64 images).
        Media parts go to the backend via llm_parts; metadata tracks attachments for history.
        """
        # Persist first (if enabled) and capture DB row id
        msg_db_id = self._persist_user_with_attachments(text, attachments_meta)

        # Build metadata for in-memory ChatMessage
        meta: Dict = {}
        if attachments_meta:
            meta["attachments"] = attachments_meta

        # Record user turn (even if text == "" for image-only)
        msg = ChatMessage(
            role="user",
            content=text or "",
            metadata=meta or None,
        )
        self._history.append(
            HistoryEntry(
                db_id=msg_db_id,
                msg=msg,
            )
        )
        self._assistant_buf = []
        self._begin_thinking_generation()

        self._active_row = self.chat.begin_assistant_stream()
        self.chat.set_streaming(True)

        # Compute persona rule injection once, in the GUI thread
        inj = self.system_injection_if_any()
        use_ham_mem = self._use_ham_mem
        memory_snapshot = self._memory_snapshot(use_ham_mem)
        media_request_options = {"temperature": 0.7}
        self._apply_model_context_allocation(media_request_options)
        requested_thinking_mode, thinking_mode = self._request_thinking_modes()

        # submit a one-off stream function that wraps the standard messages/options
        def build_messages(prompt: str) -> List[ChatMessage]:
            # Start from the raw history messages (we don't want stubs here; the
            # images are passed via llm_parts instead)
            hist_entries = self._history[-self._max_turns * 2:]
            hist = [entry.msg for entry in hist_entries]

            prefix = self._memory_prefix(prompt, memory_snapshot, inj)

            # replace the last (just-appended) user turn with a copy that has .parts
            msg = ChatMessage(role="user", content=prompt)
            setattr(msg, "parts", llm_parts)  # <-- important: keep it an object

            if hist:
                hist = [*hist[:-1], msg]
            else:
                hist = [msg]

            return [*prefix, *hist]

        def build_options() -> dict:
            return dict(media_request_options)

        stream_func = make_stream_func_from_client(
            self._model_client,
            model=self._model_name,
            build_messages=build_messages,
            build_options=build_options,
            build_thinking=lambda: thinking_mode,
            build_requested_thinking=lambda: requested_thinking_mode,
            on_final_messages=self._final_memory_snapshot,
        )

        self._active_ticket = self.broker.submit(stream_func, text)
        self._register_thinking_generation(self._active_ticket, requested_thinking_mode, thinking_mode)
        self._activate_thinking_for_ticket(self._active_ticket)

    def _on_stop(self):
        # Keep partial text visible, but reject any deltas already queued from
        # the worker after the user has cancelled this generation.
        self._thinking_ticket = -1
        self.broker.stop_active()

    def clear_memory_vectors(self) -> None:
        """Clear strict-mode derived state when the session ends or DB closes."""
        if self._memory_service:
            self._memory_service.clear_strict_vectors()

    def _on_job_token(self, ticket: int, chunk: str):
        if ticket == self._active_ticket and self._active_row is not None:
            self._assistant_buf.append(chunk)
            self.chat.stream_chunk(self._active_row, chunk)

    def _on_job_thinking(self, ticket: int, chunk: str):
        if ticket != self._active_ticket or ticket != self._thinking_ticket:
            log.debug(
                "Ignoring stale transient thinking chunk ticket=%s active_ticket=%s thinking_ticket=%s",
                ticket, self._active_ticket, self._thinking_ticket,
            )
            return
        if self._thinking_panel is None:
            return
        self._thinking_panel.append_thinking(chunk)

    def _on_job_notice(self, ticket: int, event_type: str, mode: str) -> None:
        if ticket != self._active_ticket:
            return
        if event_type == "thinking_forced_low":
            self._set_thinking_mode("low", show_forced_notice=True)
        elif event_type == "thinking_unsupported":
            generation = getattr(self, "_thinking_generation_state", {}).get(ticket)
            if generation is not None:
                generation["unsupported"] = True
                self._set_learned_thinking_capability(generation["model"], False)
            self._refresh_thinking_controls()
        elif event_type == "thinking_rejected" and self._thinking_panel is not None:
            self._thinking_panel.show_thinking_notice(
                f"Ollama rejected the {mode.title()} thinking effort. Choose another effort and retry."
            )
        elif event_type == "output_limit" and self._active_row is not None:
            # This is display-only: retain and persist any visible model text,
            # but never make the explanatory marker part of its response.
            self.chat.stream_chunk(
                self._active_row,
                "\n[output limit] The model reached its output limit. Any partial response has "
                "been kept. If Thinking consumed the budget before an answer appeared, try "
                "lowering or disabling Thinking in the Chat Panel. You can also request a "
                "shorter response and regenerate.",
            )

    def _on_job_memory_snapshot(self, ticket: int, snapshot: str) -> None:
        if ticket != self._active_ticket or self._thinking_panel is None:
            log.debug("Ignoring stale HamMem snapshot ticket=%s active_ticket=%s", ticket, self._active_ticket)
            return
        self._thinking_panel.set_memory_snapshot(snapshot)

    def _on_job_finished(self, ticket: int, status: str):
        generation = getattr(self, "_thinking_generation_state", {}).pop(ticket, None)
        if (
            status == "ok"
            and generation is not None
            and generation["unknown_at_submission"]
            and generation["explicit_think_sent"]
            and not generation["unsupported"]
        ):
            self._set_learned_thinking_capability(generation["model"], True)
        if ticket == self._active_ticket and self._active_row is not None:
            self.chat.end_assistant_stream(self._active_row)

        # Commit assistant turn iff we received any content
        if status == "ok" and self._assistant_buf:
            final_text = "".join(self._assistant_buf)
            msg_db_id: Optional[int] = None
            if self._save_enabled() and self._conv_id:
                try:
                    prof_id = None
                    try:
                        prof_id = self._session.get_profile_id() if hasattr(self._session, "get_profile_id") else None
                    except Exception:
                        prof_id = None

                    # Treat synthetic default (0) as "no profile" for storage
                    if prof_id in (0, "0"):
                        prof_id = None

                    msg_db_id = int(
                        dbo.add_message(
                            self._db,
                            conversation_id=int(self._conv_id),
                            sender_type="assistant",
                            sender_id=prof_id,
                            content=final_text,
                            metadata=None,
                        )
                    )
                except Exception:
                    msg_db_id = None

            msg = ChatMessage(role="assistant", content=final_text)
            self._history.append(
                HistoryEntry(
                    db_id=msg_db_id,
                    msg=msg,
                )
            )
        self._assistant_buf = []

        self.chat.set_streaming(False)
        self._active_row = None
        self._active_ticket = -1
        self._thinking_ticket = -1
        self._refresh_thinking_controls()
        self._refresh_ham_mem_control()

    def _on_job_error(self, ticket: int, message: str):
        if ticket == self._active_ticket and self._active_row is not None:
            # Keep visible deltas in the row, but do not add this marker to the
            # assistant buffer: errored output is never persisted as a response.
            self.chat.stream_chunk(self._active_row, f"\n[interrupted] {message}")

    # ---- Optional helpers ----
    def reset_history(self):
        """Call when starting a brand-new conversation (e.g., 'New chat')."""
        self._history.clear()
        self._assistant_buf = []
        self.clear_transient_thinking()
        self._thinking_mode = "medium"
        self._use_ham_mem = True
        self._thinking_notice_shown = False
        self._refresh_thinking_controls()
        self._refresh_ham_mem_control()
        # Drop the persisted-conversation handle; next user msg will create a new one
        self._conv_id = None
        self._clear_thinking()

    def has_persisted_conversation(self) -> bool:
        """
        Return True if this controller is currently attached to a saved_conversations row.
        """
        return self._conv_id is not None

    def current_conversation_id(self) -> Optional[int]:
        """Return the active conversation_id, or None if unsaved/guest/admin."""
        return self._conv_id

    def base_index_for_message_id(self, message_id: int) -> Optional[int]:
        """
        Return the in-memory history index for a given DB message id.
        """
        if message_id is None:
            return None
        try:
            needle = int(message_id)
        except Exception:
            return None
        for idx, entry in enumerate(self._history):
            if entry.db_id is not None and int(entry.db_id) == needle:
                return idx
        return None

    def load_conversation(self, conversation_id: int, messages: list[dict]) -> None:
        """
        Attach the controller to an existing saved conversation.
        `messages` should be rows from db_ops.list_messages().
        """
        self._history.clear()
        self._assistant_buf = []
        self._conv_id = int(conversation_id)
        self.clear_transient_thinking()
        try:
            self._thinking_mode = dbo.get_conversation_thinking_mode(self._db, self._conv_id) if self._db else "medium"
            self._use_ham_mem = dbo.get_conversation_use_ham_mem(self._db, self._conv_id) if self._db else True
        except Exception:
            self._thinking_mode = "medium"
            self._use_ham_mem = True
        self._thinking_notice_shown = False
        self._refresh_thinking_controls()
        self._refresh_ham_mem_control()

        insert_offset = 0  # tracks extra rows added for thumbs so indices stay aligned
        ui_row = -1

        for m in messages:
            msg_db_id = m.get("id")
            sender = m.get("sender_type", "assistant")
            text = m.get("content", "") or ""
            metadata = m.get("metadata") or {}
            metadata = dict(metadata)
            attachments = metadata.get("attachments") or []
            if not text and not attachments:
                continue

            if sender == "user":
                role = "user"
            elif sender == "system":
                role = "system"
            else:
                role = "assistant"

            # Always put the logical message into _history if there's text or attachments.
            # This keeps LLM context consistent for reloads.
            msg = ChatMessage(role=role, content=text or "", metadata=metadata or None)
            self._history.append(
                HistoryEntry(
                    db_id=int(msg_db_id) if msg_db_id is not None else None,
                    msg=msg,
                )
            )

            # UI row index is only advanced when there's a visible text bubble
            if text:
                ui_row += 1

            if role == "user" and attachments and getattr(self, "chat", None):
                thumb_paths: list[str] = []
                for att in attachments:
                    if isinstance(att, dict):
                        thumb_id = att.get("thumb_file_id")
                        file_id = att.get("file_id")
                        path = None
                        if thumb_id is not None and self._db is not None:
                            try:
                                path = dbo.cas_path_for_file(self._db, int(thumb_id), data_dir=self._data_dir)
                            except Exception:
                                path = None
                        if path is None and file_id is not None and self._db is not None:
                            try:
                                path = dbo.cas_path_for_file(self._db, int(file_id), data_dir=self._data_dir)
                            except Exception:
                                path = None
                        if path:
                            thumb_paths.append(str(path))
                    elif isinstance(att, str):
                        # Legacy metadata: att is already a filesystem path or file:// URL
                        thumb_paths.append(att)

                if thumb_paths:
                    try:
                        target_row = ui_row + insert_offset
                        self.chat.insert_thumbs_after(target_row, thumb_paths)
                        insert_offset += 1
                    except Exception:
                        pass

    def resend_message(self, index: int):
        """Resend from this user message: truncate after it, then replay it."""
        payload = self._get_user_payload(index)
        if not payload:
            return

        text = payload.get("text") or ""
        attachments = self._resolve_attachment_paths(payload)
        msg_id = payload.get("message_id")
        base_index = payload.get("base_index")

        # 1) Persisted truncate (if we have a real conversation + message id)
        if self._save_enabled() and self._db is not None and self._conv_id and msg_id:
            try:
                dbo.delete_many_messages(
                    self._db,
                    conversation_id=int(self._conv_id),
                    message_id=int(msg_id),
                )
            except Exception:
                pass

        # Keep in-memory history consistent with DB
        try:
            self._truncate_history_from_message_id(int(msg_id))
        except Exception:
            pass

        # 2) Update the UI to remove all message bubbles from this logical message onward
        if isinstance(base_index, int):
            try:
                self.chat.truncate_messages_from(base_index)
            except Exception:
                pass

        # 3) Resend payload
        if attachments:
            self._send_with_attachments(text, attachments)
        else:
            if not text:
                return
            # Append a new user bubble and stream as usual
            self.chat.append_message("user", text)
            self._on_user_text(text)

    def regenerate_from(self, index: int):
        """
        Regenerate an assistant reply by treating this as a 'resend' of the
        corresponding user message. The ChatDisplay payload resolver will walk
        backwards from this bubble to find the right user turn.
        """
        self.resend_message(index)

    def prepare_edit_resend(self, index: int) -> Optional[dict]:
        """
        Prepare an 'edit & resend' operation:

        - Find the logical user payload for this bubble index
          (text + attachments + db message id).
        - Truncate the DB conversation tail starting at that message.
        - Truncate the UI bubbles from the logical base_index onward.
        - Return a payload dict with 'text' and 'attachments' for the caller
          to stuff back into the input field.

        Unlike resend_message(), this does NOT actually send anything.
        """
        payload = self._get_user_payload(index)
        if not payload:
            return None

        text = payload.get("text") or ""
        attachments = self._resolve_attachment_paths(payload)
        msg_id = payload.get("message_id")
        base_index = payload.get("base_index")

        # 1) Persisted truncate (if we have a real conversation + message id)
        if self._save_enabled() and self._db is not None and self._conv_id and msg_id:
            try:
                dbo.delete_many_messages(
                    self._db,
                    conversation_id=int(self._conv_id),
                    message_id=int(msg_id),
                )
            except Exception:
                pass

        try:
            self._truncate_history_from_message_id(int(msg_id))
        except Exception:
            pass

        # 2) Update the UI to remove all message bubbles from this logical message onward
        if isinstance(base_index, int):
            try:
                self.chat.truncate_messages_from(base_index)
            except Exception:
                pass

        return {
            "text": text,
            "attachments": attachments,
        }

    def fork_chat_at(self, index: int):
        """
        Fork the current conversation at a given bubble index.

        - If the selected bubble belongs to a *user* message:
            * New conversation is created with history up to *before* that user message.
            * The selected user message's text + attachments are put into the input
              (like edit_resend), ready to send in the fork.

        - If the selected bubble is an *assistant* message:
            * New conversation is created with history up to and including that
              assistant message.
        """
        # Must be a real, persisted user conversation, otherwise we're just resending the message.
        if not self._save_enabled() or not self._db or not self._conv_id:
            self.resend_message(index)
            return
        if not self._session or self._session.current.user_id is None:
            return

        # Get the raw payload so we know which role we're forking on.
        try:
            if not hasattr(self.chat, "get_message_payload"):
                return
            raw_payload = self.chat.get_message_payload(index)
        except Exception:
            return

        if not raw_payload:
            return

        role = raw_payload.get("role") or ""
        if role not in ("user", "assistant"):
            # Only user/assistant bubbles make sense to fork from.
            return

        # For user forks we also want the logical user payload (text + attachments + msg_id)
        user_payload: Optional[dict] = None
        pivot_msg_id: Optional[int] = None

        if role == "user":
            # This resolves image bubbles into their logical text+attachments user turn.
            user_payload = self._get_user_payload(index)
            if not user_payload or user_payload.get("role") != "user":
                return
            pivot_msg_id = user_payload.get("message_id")
            base_index = user_payload.get("base_index")
        else:
            # Assistant fork: pivot is the assistant message itself.
            base_index = raw_payload.get("base_index", index)

        # Map base_index → HistoryEntry → db_id, if we don't already have it.
        if pivot_msg_id is None:
            if not isinstance(base_index, int):
                return
            if not (0 <= base_index < len(self._history)):
                return
            entry = self._history[base_index]
            pivot_msg_id = entry.db_id

        if pivot_msg_id is None:
            # Message isn't in DB (unsaved / ephemeral thread) → nothing to fork.
            return

        # Create the forked conversation with an appropriate title.
        uid = int(self._session.current.user_id)  # type: ignore
        new_title = self._make_fork_title()
        try:
            new_conv_id = dbo.create_conversation(
                self._db, user_id=uid, title=new_title, thinking_mode=self._thinking_mode, use_ham_mem=self._use_ham_mem,
            )
        except Exception:
            return

        # Copy messages from the old conversation up to the pivot.
        include_pivot = (role == "assistant")
        try:
            rows = dbo.list_messages(self._db, int(self._conv_id), limit=1000000)
        except Exception:
            rows = []

        for row in rows:
            mid = row.get("id")
            if mid is None:
                continue

            if mid < pivot_msg_id:
                self._clone_message_to_conversation(new_conv_id, row)
            elif mid == pivot_msg_id:
                if include_pivot:
                    self._clone_message_to_conversation(new_conv_id, row)
                break
            else:
                break

        # Load the new conversation into controller + UI.
        try:
            new_rows = dbo.list_messages(self._db, int(new_conv_id), limit=1000000)
        except Exception:
            new_rows = []

        # Notify UI: new conversation exists & should be opened.
        # conversation_started → refresh chats list / badges.
        try:
            self.conversation_started.emit(int(new_conv_id))
        except Exception:
            pass

        # forked_conversation → MainWindow._open_conversation (draw bubbles + attach controller)
        try:
            self.forked_conversation.emit(int(new_conv_id))
        except Exception:
            pass

        # For user forks: behave like edit_resend on the forked convo:
        # pre-fill the input and pending attachments, but don't send yet.
        if role == "user" and user_payload:
            text = user_payload.get("text") or ""
            attachments = self._resolve_attachment_paths(user_payload)
            try:
                self.chat.input.setPlainText(text)
            except Exception:
                pass
            try:
                if hasattr(self.chat, "set_pending_attachments"):
                    self.chat.set_pending_attachments(attachments)
            except Exception:
                pass

    def _truncate_history_from_message_id(self, message_id: int) -> None:
        """
        Drop all history entries whose db_id is >= message_id.
        Keeps in-memory context aligned with the DB after truncation.
        """
        if not self._history:
            return

        cutoff = None
        for i, entry in enumerate(self._history):
            db_id = entry.db_id
            if db_id is not None and db_id >= message_id:
                cutoff = i
                break

        if cutoff is not None:
            self._history = self._history[:cutoff]

    def _attachment_stub_for_model(self, attachments: list) -> str:
        """
        Build a short textual stub describing attachments for the LLM history, e.g.
        "[User attached 2 image(s)]". Uses MIME buckets only.
        """
        if not attachments:
            return ""

        counts = {"image": 0, "audio": 0, "video": 0, "text": 0, "other": 0}

        for att in attachments:
            if isinstance(att, dict):
                mime = (att.get("mime") or att.get("mime_type") or "").lower()
                if mime.startswith("image/"):
                    counts["image"] += 1
                elif mime.startswith("audio/"):
                    counts["audio"] += 1
                elif mime.startswith("video/"):
                    counts["video"] += 1
                elif mime.startswith("text/"):
                    counts["text"] += 1
                else:
                    counts["other"] += 1
            else:
                counts["other"] += 1

        parts = []
        if counts["image"]:
            parts.append(f"{counts['image']} image(s)")
        if counts["audio"]:
            parts.append(f"{counts['audio']} audio file(s)")
        if counts["video"]:
            parts.append(f"{counts['video']} video file(s)")
        if counts["text"]:
            parts.append(f"{counts['text']} text file(s)")
        if counts["other"]:
            parts.append(f"{counts['other']} other file(s)")

        if not parts:
            return ""

        return "[User attached " + ", ".join(parts) + "]"

    # ---- helpers for bubble actions ----------------------------------------
    def _get_user_payload(self, index: int) -> Optional[dict]:
        """
        Ask the ChatDisplay for the logical payload for a bubble index, then
        enrich it with our DB message id (if we have one) via HistoryEntry.

        We assume ChatDisplay.get_user_payload(index) returns at least:
            {
                "role": "user" | "assistant" | ...,
                "text": str,
                "attachments": list,
                "base_index": int,   # logical message index in history
                ...
            }
        """
        try:
            if not hasattr(self.chat, "get_user_payload"):
                return None
            payload = self.chat.get_user_payload(index)
        except Exception:
            return None

        if not payload:
            return None

        # Only care about user messages for delete/resend/regenerate.
        if payload.get("role") != "user":
            return payload

        base_index = payload.get("base_index")
        if isinstance(base_index, int):
            if 0 <= base_index < len(self._history):
                entry = self._history[base_index]
                if entry.db_id is not None:
                    # Attach the DB id so callers can use it.
                    payload["message_id"] = entry.db_id
                meta = getattr(entry.msg, "metadata", None) or {}
                try:
                    atts_meta = meta.get("attachments")
                    if atts_meta:
                        payload["attachments_meta"] = atts_meta
                except Exception:
                    pass

        return payload

    def _resolve_attachment_paths(self, payload: dict) -> list[str]:
        """
        Resolve filesystem paths for original attachments, preferring DB-backed metadata.
        """
        paths: list[str] = []
        try:
            atts_meta = payload.get("attachments_meta")
        except Exception:
            atts_meta = None

        if isinstance(atts_meta, list):
            for att in atts_meta:
                if not isinstance(att, dict):
                    continue
                fid = att.get("file_id")
                if fid is None or self._db is None:
                    continue
                try:
                    path = dbo.cas_path_for_file(self._db, int(fid), data_dir=self._data_dir)
                except Exception:
                    path = None
                if path:
                    paths.append(str(path))

        if not paths:
            try:
                fallback = payload.get("attachments") or []
            except Exception:
                fallback = []
            for att in fallback:
                if isinstance(att, str):
                    paths.append(str(att))

        return paths

    def _send_with_attachments(self, text: str, attachments: list[str]) -> None:
        if not attachments:
            return
        if text:
            self.chat.append_message("user", text)
        # If vision is unavailable, fall back to text-only send.
        if not getattr(getattr(self._session, "current", None), "vision", False):
            self._on_user_text(text)
            return

        try:
            batch = process_images(
                attachments,
                ephemeral=(getattr(getattr(self._session, "current", None), "role", "guest") != "user"),
                db=self._db,
                data_dir=self._data_dir,
                session=self._session,
            )
            parts = batch["llm_parts"]
            thumb_paths = [t["path"] for t in batch.get("thumbs", [])]
            attachments_meta = []
            for stored, thumb in zip(batch.get("stored", []), batch.get("thumbs", [])):
                attachments_meta.append({
                    "file_id": stored["file_id"],
                    "sha256": stored["sha256"],
                    "mime": stored["mime"],
                    "thumb_file_id": thumb.get("file_id"),
                    "thumb_sha256": thumb.get("sha256"),
                })
        except Exception:
            parts, thumb_paths, attachments_meta = [], [], []

        if not parts:
            self._on_user_text(text)
            return

        if thumb_paths:
            self.chat.draw_thumbs(thumb_paths)
        self.send_user_with_media(text, parts, attachments_meta or None)

    def _regenerate_text_only(self, text: str) -> None:
        if not text:
            return
        regen_msg = ChatMessage(role="user", content=text)
        self._history.append(
            HistoryEntry(
                db_id=None,          # this regen helper user message is not a new DB row
                msg=regen_msg,
            )
        )

        inj = self.system_injection_if_any()
        stream_func = self._make_text_stream(inj, {"temperature": 0.7}, self._memory_snapshot(self._use_ham_mem))
        self._assistant_buf = []
        self._begin_thinking_generation()
        self._active_row = self.chat.begin_assistant_stream()
        self.chat.set_streaming(True)
        self._active_ticket = self.broker.submit(stream_func, text)
        self._activate_thinking_for_ticket(self._active_ticket)

    def _regenerate_with_attachments(self, text: str, attachments: list[str]) -> None:
        if not getattr(getattr(self._session, "current", None), "vision", False):
            # Fallback: regenerate as text-only.
            self._regenerate_text_only(text)
            return

        try:
            batch = process_images(
                attachments,
                ephemeral=(getattr(getattr(self._session, "current", None), "role", "guest") != "user"),
                db=self._db,
                data_dir=self._data_dir,
                session=self._session,
            )
            parts = batch["llm_parts"]
            attachments_meta = []
            for stored, thumb in zip(batch.get("stored", []), batch.get("thumbs", [])):
                attachments_meta.append({
                    "file_id": stored["file_id"],
                    "sha256": stored["sha256"],
                    "mime": stored["mime"],
                    "thumb_file_id": thumb.get("file_id"),
                    "thumb_sha256": thumb.get("sha256"),
                })
        except Exception:
            parts, attachments_meta = [], []

        if not parts:
            # No usable media; fall back to text-only regen
            self._regenerate_text_only(text)
            return

        # Run a media-enabled request without adding new user bubbles to the UI.
        self.send_user_with_media(text, parts, attachments_meta or None)

    def hard_kill(self) -> bool:
        """
        Best-effort kill switch for any background LLM work.
        Intended to be called from MainWindow.closeEvent before shutdown.
        """
        try:
            # Stop anything in-flight and clear any queued jobs
            if getattr(self, "broker", None) is not None:
                # This clears queued jobs and, if include_active=True,
                # calls stop_active() on the running worker.
                self.broker.clear_queue(include_active=True)

            # Defensive: reset controller state
            self._assistant_buf = []
            self._active_row = None
            self._active_ticket = -1
            self.clear_transient_thinking()

            # Tell the UI we're no longer streaming (just in case)
            try:
                self.chat.set_streaming(False)
            except Exception:
                pass

            return True
        except Exception as e:
            return False
