# hamchat/ui/chat_controller.py
from __future__ import annotations
from typing import Optional, List, Dict
from PyQt6.QtCore import QObject, Qt, pyqtSignal

from hamchat.infra.llm.thread_broker import ThreadBroker
from hamchat.infra.llm.base import ChatMessage
from hamchat.infra.llm.backend_adapter import make_stream_func_from_client
from hamchat import db_ops as dbo  # persistence API (create_conversation, add_message)
from hamchat.core.session import SessionManager
from hamchat.media_helper import process_images

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
        self._history: List[ChatMessage] = []
        self._assistant_buf: List[str] = []
        self._max_turns: int = 512   # rolling window; adjust as needed
        # ToDo set the max turns in the session, load it from app.json, or infer it from spec report maybe

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

    def _persist_user_with_attachments(self, text: str, attachments_meta: Optional[List[Dict]] = None) -> None:
        if not self._save_enabled():
            return
        try:
            self._ensure_conversation(text)
            if not self._conv_id:
                return
            uid = int(self._session.current.user_id)  # type: ignore
            dbo.add_message(
                self._db,
                conversation_id=int(self._conv_id),
                sender_type="user",
                sender_id=uid,
                content=text,
                metadata={"attachments": attachments_meta} if attachments_meta else None,
            )
        except Exception:
            # do not break UX if persistence fails
            pass

    # ---------- Configuration ----------

    def _configure_stream(self) -> None:
        """
        (Re)build the stream_func with the current model.
        Called on init and whenever set_model_name is used.
        """

        def _build_messages(prompt: str) -> List[ChatMessage]:
            hist: List[ChatMessage] = []

            for m in self._history[-self._max_turns * 2:]:
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
        # Record user turn into the rolling history
        self._history.append(ChatMessage(role="user", content=text))
        self._assistant_buf = []  # reset buffer for this turn

        # --- Persistence: create conversation (first turn) + save user message
        if self._save_enabled():
            try:
                # Create conversation on first user msg
                self._ensure_conversation(text)
                if self._conv_id:
                    # optional metadata: include pending attachments if any
                    attachments = self.chat.get_pending_attachments() if hasattr(self.chat, "get_pending_attachments") else []
                    dbo.add_message(
                        self._db,
                        conversation_id=self._conv_id,
                        sender_type="user",
                        sender_id=int(self._session.current.user_id),  # type: ignore
                        content=text,
                        metadata={"attachments": attachments} if attachments else None,
                    )
            except Exception:
                # ignore persistence errors; keep chat flowing
                pass

        # Prepare UI row and kick off background job
        self._active_row = self.chat.begin_assistant_stream()
        self.chat.set_streaming(True)
        self._active_ticket = self.broker.submit(self.stream_func, text)

    def send_user_with_media(self, text: str, llm_parts: List[Dict], attachments_meta: Optional[List[Dict]] = None):
        """
        Send a user turn that includes vision parts (base64 images).
        Media parts go to the backend via llm_parts; metadata tracks attachments for history.
        """
        meta = {"attachments": attachments_meta} if attachments_meta else None

        # Record user turn (even if text == "" for image-only)
        self._history.append(ChatMessage(role="user", content=text or "", metadata=meta))
        self._assistant_buf = []

        self._persist_user_with_attachments(text, attachments_meta)

        self._active_row = self.chat.begin_assistant_stream()
        self.chat.set_streaming(True)

        # submit a one-off stream function that wraps the standard messages/options
        def build_messages(prompt: str) -> List[ChatMessage]:
            hist = self._history[-self._max_turns * 2:]
            # replace the last (just-appended) user turn with a copy that has .parts
            msg = ChatMessage(role="user", content=prompt)
            setattr(msg, "parts", llm_parts)  # <-- important: keep it an object
            return [*hist[:-1], msg]

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
            self._history.append(ChatMessage(role="assistant", content=final_text))
            # --- Persistence: save assistant message
            if self._save_enabled() and self._conv_id:
                try:
                    dbo.add_message(
                        self._db,
                        conversation_id=self._conv_id,
                        sender_type="assistant",
                        sender_id=None,
                        content=final_text,
                        metadata=None,
                    )
                except Exception:
                    pass
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
            sender = m.get("sender_type", "assistant")
            text = m.get("content", "") or ""
            metadata = m.get("metadata") or {}
            attachments = metadata.get("attachments") or []
            if not text and not attachments:
                continue

            if sender == "user":
                role = "user"
            elif sender == "system":
                role = "system"
            else:
                # treat tool/assistant/anything-else as assistant in the LLM context
                role = "assistant"

            # Always put the logical message into _history if there's text or attachments.
            # This keeps LLM context consistent for reloads.
            self._history.append(ChatMessage(role=role, content=text or "", metadata=metadata))

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
        """MVP: called when user chooses 'Resend' from a bubble context menu."""
        payload = self._get_user_payload(index)
        if not payload:
            return
        text = payload.get("text") or ""
        attachments = payload.get("attachments") or []

        if attachments:
            self._send_with_attachments(text, attachments)
        else:
            if not text:
                return
            self.chat.append_message("user", text)
            self._on_user_text(text)

    def regenerate_from(self, index: int):
        """MVP: regenerate the assistant response for the user message at this index."""
        payload = self._get_user_payload(index)
        if not payload:
            # Fallback: last user message in history
            for m in reversed(self._history):
                if m.role == "user":
                    payload = {"text": m.content, "attachments": []}
                    break
        if not payload:
            return

        text = payload.get("text") or ""
        attachments = payload.get("attachments") or []
        if not text:
            return

        # Do not add another user bubble; just start a new assistant stream using the same prompt.
        if attachments:
            self._regenerate_with_attachments(text, attachments)
            return

        self._regenerate_text_only(text)

    def fork_chat_at(self, index: int):
        """MVP: create a new chat from history up to this index."""
        # TODO: create a new conversation cloned up to this logical message, then switch UI to it.
        print(f"Fork requested at bubble index {index}")

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
        try:
            if hasattr(self.chat, "get_user_payload"):
                return self.chat.get_user_payload(index)
        except Exception:
            return None
        return None

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
        self._history.append(regen_msg)

        def _build_messages(_prompt: str) -> List[ChatMessage]:
            hist: List[ChatMessage] = []
            for m in self._history[-self._max_turns * 2:]:
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
