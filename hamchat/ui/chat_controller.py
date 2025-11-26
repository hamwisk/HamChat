# hamchat/ui/chat_controller.py
from __future__ import annotations
from typing import Optional, List, Dict
from PyQt6.QtCore import QObject, Qt, pyqtSignal

from hamchat.infra.llm.thread_broker import ThreadBroker
from hamchat.infra.llm.base import ChatMessage
from hamchat.infra.llm.backend_adapter import make_stream_func_from_client
from hamchat import db_ops as dbo  # persistence API (create_conversation, add_message)
from hamchat.core.session import SessionManager

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
        self._max_turns: int = 64   # rolling window; adjust as needed
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

    # ---------- Configuration ----------

    def _configure_stream(self) -> None:
        """
        (Re)build the stream_func with the current model.
        Called on init and whenever set_model_name is used.
        """

        def _build_messages(prompt: str) -> List[ChatMessage]:
            # window: keep last N turns (system messages optional; add if you have them)
            hist = self._history[-self._max_turns * 2:]  # user+assistant pairs
            return [*hist, ChatMessage(role="user", content=prompt)]

        def _build_options() -> dict:
            return {"temperature": 0.7}
            # ToDo expose this value to a slider or other suitable QObject in the AI profiles manager and load it from the session

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

    def send_user_with_media(self, text: str, llm_parts: List[Dict]):
        """
        Send a user turn that includes vision parts (base64 images).
        Keeps history text-only; media parts are passed just-in-time to the backend.
        """
        # record user text into rolling history (same as _on_user_text)
        self._history.append(ChatMessage(role="user", content=text))
        self._assistant_buf = []
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

        for m in messages:
            sender = m.get("sender_type", "assistant")
            text = m.get("content", "") or ""
            if not text:
                continue

            if sender == "user":
                role = "user"
            elif sender == "system":
                role = "system"
            else:
                # treat tool/assistant/anything-else as assistant in the LLM context
                role = "assistant"

            self._history.append(ChatMessage(role=role, content=text))

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

            print("ChatController.hard_kill(): broker queue cleared")
            return True
        except Exception as e:
            print(f"ChatController.hard_kill(): error during shutdown: {e}")
            return False
