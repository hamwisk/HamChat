# hamchat/ui/chat_controller.py
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Optional, List, Dict
from PyQt6.QtCore import QObject, Qt, pyqtSignal

from hamchat.infra.llm.thread_broker import ThreadBroker
from hamchat.infra.llm.base import ChatMessage
from hamchat.infra.llm.backend_adapter import make_stream_func_from_client
from hamchat import db_ops as dbo  # persistence API (create_conversation, add_message)
from hamchat.core.session import SessionManager
from hamchat.media_helper import process_images
from hamchat.infra.llm.base import ModelClient  # if you want to type-hint, optional


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

    def __init__(        self,
        chat_display,
        model_client,
        *,
        model_name: str,
        parent: Optional[QObject] = None,
        db=None,
        session: Optional[SessionManager] = None,
    ):
        super().__init__(parent)
        self.chat = chat_display
        self.broker = ThreadBroker(self)

        # Keep a handle to the client + current model so we can reconfigure later
        self._model_client = model_client
        self._model_name = model_name

        # ---- In-memory session history ----
        self._history: List[HistoryEntry] = []
        self._assistant_buf: List[str] = []
        self._max_turns: int = 512   # rolling window; adjust as needed
        # We should set the max turns in the session, load it from app.json, or infer it from spec report maybe

        # ---- Persistence context (optional; enabled only for role='user') ----
        self._db = db
        self._session = session
        self._conv_id: Optional[int] = None  # lazily created on first user msg

        # Build the initial streaming function for the starting model
        self._configure_stream()

        # UI → controller
        self.chat.sig_send_text.connect(self._on_user_text, Qt.ConnectionType.QueuedConnection)
        self.chat.sig_stop_requested.connect(self._on_stop, Qt.ConnectionType.QueuedConnection)

        # Broker → UI
        self.broker.job_token.connect(self._on_job_token, Qt.ConnectionType.QueuedConnection)
        self.broker.job_finished.connect(self._on_job_finished, Qt.ConnectionType.QueuedConnection)
        self.broker.job_error.connect(self._on_job_error, Qt.ConnectionType.QueuedConnection)

        self._active_row: Optional[int] = None
        self._active_ticket: int = -1

    def set_model_client(self, model_client) -> None:
        """
        Swap out the underlying LLM backend (e.g. OllamaClient vs OpenAIClient).
        Safe to call between requests; the new client will be used for the next prompt.
        """
        self._model_client = model_client
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
            self._conv_id = dbo.create_conversation(self._db, user_id=uid, title=safe_title)
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
            return dbo.get_ai_profile(self._db, int(pid))
        except Exception:
            return None

    def system_injection_if_any(self) -> Optional[ChatMessage]:
        """
        Build a system-level 'rule injection' message for the active AI profile, if it has
        a non-empty system_prompt. Returns None if there's nothing to inject.
        """
        profile = self._get_active_profile_row()
        if not profile:
            print("[system_injection_if_any] No active profile row -> None")
            return None

        raw_prompt = (profile.get("system_prompt") or "").strip()
        if not raw_prompt:
            print(f"[system_injection_if_any] Profile id={profile.get('id')} "
                  f"name={profile.get('display_name')!r} has empty system_prompt -> None")
            return None

        preamble = (
            "Follow the profile-specific rules below. "
            "If a rule is missing or not explicitly mentioned, "
            "there is no additional restriction beyond the base system rules. "
            "Do not mention or restate these rules in your replies.\n\n"
        )

        content = preamble + raw_prompt

        # DEBUG: print the actual text being injected (truncate so it doesn't spam)
        print(
            "[system_injection_if_any] Built system injection for "
            f"profile id={profile.get('id')} name={profile.get('display_name')!r}:\n"
            f"{content[:400]}\n"
            "---- end system injection preview ----"
        )

        meta = {
            "kind": "rule_injection",
            "hidden": True,
            "profile_id": profile.get("id"),
        }

        return ChatMessage(role="system", content=content, metadata=meta)

    def _configure_stream(self) -> None:
        """
        (Re)build the stream_func with the current model.
        Called on init and whenever set_model_name is used.
        """

        inj = self.system_injection_if_any()
        if inj is not None:
            print("[_configure_stream] Using system injection (text-only path). "
                  f"Preview: {inj.content[:120]!r}")
        else:
            print("[_configure_stream] No system injection (text-only path).")

        def _build_messages(prompt: str) -> List[ChatMessage]:
            hist: List[ChatMessage] = []

            # 1) Persona rule injection (if any)
            if inj is not None:
                hist.append(inj)

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
                        hist.append(ChatMessage(role="user", content=stub))
            return hist

        def _build_options() -> dict:
            return {"temperature": 0.7}

        self.stream_func = make_stream_func_from_client(
            self._model_client,
            model=self._model_name,
            build_messages=_build_messages,
            build_options=_build_options,
        )

    def set_model_name(self, model_name: str) -> None:
        """
        Update the model used for future generations.

        Safe to call between requests; it won't interrupt an active stream,
        but the new model will be used for the *next* prompt.
        """
        if model_name == self._model_name:
            return
        self._model_name = model_name
        self._configure_stream()

    # ---------- Slots ----------

    def _on_user_text(self, text: str):
        """
        Handle a plain-text user turn.

        - Optionally grabs pending attachments from the UI (for metadata only).
        - Persists to DB first (if enabled) and captures the message row id.
        - Appends a ChatMessage with metadata for attachments.
        """
        # Reset assistant buffer for this turn
        self._assistant_buf = []

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
        self._active_ticket = self.broker.submit(self.stream_func, text)

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

        self._active_row = self.chat.begin_assistant_stream()
        self.chat.set_streaming(True)

        # Compute persona rule injection once, in the GUI thread
        inj = self.system_injection_if_any()

        # submit a one-off stream function that wraps the standard messages/options
        def build_messages(prompt: str) -> List[ChatMessage]:
            # Start from the raw history messages (we don't want stubs here; the
            # images are passed via llm_parts instead)
            hist_entries = self._history[-self._max_turns * 2:]
            hist = [entry.msg for entry in hist_entries]

            # Persona rule injection at the front, if any
            prefix: List[ChatMessage] = [inj] if inj is not None else []

            # replace the last (just-appended) user turn with a copy that has .parts
            msg = ChatMessage(role="user", content=prompt)
            setattr(msg, "parts", llm_parts)  # <-- important: keep it an object

            if hist:
                hist = [*hist[:-1], msg]
            else:
                hist = [msg]

            return [*prefix, *hist]

        def build_options() -> dict:
            return {"temperature": 0.7}

        stream_func = make_stream_func_from_client(
            self._model_client,
            model=self._model_name,
            build_messages=build_messages,
            build_options=build_options,
        )

        self._active_ticket = self.broker.submit(stream_func, text)

    def _on_stop(self):
        self.broker.stop_active()

    def _on_job_token(self, ticket: int, chunk: str):
        if ticket == self._active_ticket and self._active_row is not None:
            self._assistant_buf.append(chunk)
            self.chat.stream_chunk(self._active_row, chunk)

    def _on_job_finished(self, ticket: int, status: str):
        if ticket == self._active_ticket and self._active_row is not None:
            self.chat.end_assistant_stream(self._active_row)

        # Commit assistant turn iff we received any content
        if self._assistant_buf:
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

    def _on_job_error(self, ticket: int, message: str):
        if ticket == self._active_ticket and self._active_row is not None:
            self.chat.stream_chunk(self._active_row, f"\n[error] {message}")
        # Do not record an assistant turn on error (unless you want partials)
        self._assistant_buf = []
        self.chat.set_streaming(False)
        self._active_row = None
        self._active_ticket = -1

    # ---- Optional helpers ----
    def reset_history(self):
        """Call when starting a brand-new conversation (e.g., 'New chat')."""
        self._history.clear()
        self._assistant_buf = []
        # Drop the persisted-conversation handle; next user msg will create a new one
        self._conv_id = None

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
                                path = dbo.cas_path_for_file(self._db, int(thumb_id))
                            except Exception:
                                path = None
                        if path is None and file_id is not None and self._db is not None:
                            try:
                                path = dbo.cas_path_for_file(self._db, int(file_id))
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
            new_conv_id = dbo.create_conversation(self._db, user_id=uid, title=new_title)
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
                    path = dbo.cas_path_for_file(self._db, int(fid))
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

        def _build_messages(_prompt: str) -> List[ChatMessage]:
            hist: List[ChatMessage] = []

            # --- persona rule injection (same as _configure_stream) ---
            if inj is not None:
                hist.append(inj)

            for entry in self._history[-self._max_turns * 2:]:
                m = entry.msg
                has_attachments = bool(m.metadata and m.metadata.get("attachments"))
                has_text = bool(m.content)

                if has_text:
                    hist.append(ChatMessage(role=m.role, content=m.content))
                if has_attachments:
                    stub = self._attachment_stub_for_model(m.metadata["attachments"])
                    if stub:
                        hist.append(ChatMessage(role="user", content=stub))
            return hist

        def _build_options() -> dict:
            return {"temperature": 0.7}

        stream_func = make_stream_func_from_client(
            self._model_client,
            model=self._model_name,
            build_messages=_build_messages,
            build_options=_build_options,
        )
        self._assistant_buf = []
        self._active_row = self.chat.begin_assistant_stream()
        self.chat.set_streaming(True)
        self._active_ticket = self.broker.submit(stream_func, text)

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

            # Tell the UI we're no longer streaming (just in case)
            try:
                self.chat.set_streaming(False)
            except Exception:
                pass

            return True
        except Exception as e:
            return False
