from __future__ import annotations

import itertools
import logging
from PyQt6.QtCore import Qt, QObject, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QListWidget, QMessageBox, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from hamchat import db_ops
from hamchat.memory_embeddings import MemoryEmbeddingService, OllamaEmbeddingProvider
log = logging.getLogger("memory.manager")
_ACTIVE_EMBEDDING_JOBS = {}
_JOB_IDS = itertools.count(1)

def _finish_job(job_id: int) -> None:
    _ACTIVE_EMBEDDING_JOBS.pop(job_id, None)
    log.debug("Embedding thread finished and cleaned up id=%s", job_id)

class _EmbeddingWorker(QObject):
    done = pyqtSignal(int, int, int, str, object, str)
    def __init__(self, job_id, generation, memory_id, content, fingerprint, provider):
        super().__init__(); self.job_id, self.generation, self.memory_id, self.content, self.fingerprint, self.provider = job_id, generation, memory_id, content, fingerprint, provider
    def run(self):
        log.debug("Embedding job started id=%s memory_id=%s provider=%s model=%s", self.job_id, self.memory_id, self.provider.provider_id, self.provider.model_id)
        try:
            vector = self.provider.embed_one(self.content)
            log.debug("Embedding job succeeded id=%s memory_id=%s", self.job_id, self.memory_id)
            self.done.emit(self.job_id, self.generation, self.memory_id, self.fingerprint, vector, "")
        except Exception as exc:
            log.warning("Embedding job failed id=%s memory_id=%s error=%s", self.job_id, self.memory_id, type(exc).__name__)
            self.done.emit(self.job_id, self.generation, self.memory_id, self.fingerprint, None, str(exc))


class MemoryManager(QWidget):
    """GUI-thread editor for manually managed HamMem records."""

    sig_close = pyqtSignal()

    def __init__(self, conn, session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._conn = conn
        self._session = session
        self._memory_id: int | None = None
        self._loading = False
        self._embedding_service = MemoryEmbeddingService(conn, OllamaEmbeddingProvider())
        self._embedding_thread = None
        self._embedding_generation = 0
        self._rebuild_active = False
        self.destroyed.connect(self._invalidate_embedding_work)
        if hasattr(session, "sessionChanged"):
            session.sessionChanged.connect(self._invalidate_embedding_work)

        root = QHBoxLayout(self); root.setContentsMargins(12, 12, 12, 12); root.setSpacing(12)
        self.memory_list = QListWidget(self); self.memory_list.setMinimumWidth(240)
        self.memory_list.currentItemChanged.connect(self._select_memory)
        root.addWidget(self.memory_list, 1)

        detail = QFrame(self); detail_layout = QVBoxLayout(detail); detail_layout.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout(); header.addWidget(QLabel("<b>Memory Manager</b>", detail)); header.addStretch(1)
        self.new_button = QPushButton("New", detail); self.save_button = QPushButton("Save", detail)
        self.delete_button = QPushButton("Delete", detail); close_button = QPushButton("Close", detail)
        self.new_button.clicked.connect(self._new_memory); self.save_button.clicked.connect(self._save_memory)
        self.delete_button.clicked.connect(self._delete_memory); close_button.clicked.connect(self._close)
        for button in (self.new_button, self.save_button, self.delete_button, close_button): header.addWidget(button)
        detail_layout.addLayout(header)

        self.status = QLabel("", detail); self.status.setWordWrap(True); detail_layout.addWidget(self.status)
        self.embedding_status = QLabel("Embeddings: Ollama / nomic-embed-text", detail); detail_layout.addWidget(self.embedding_status)
        self.rebuild_button = QPushButton("Rebuild embeddings", detail); self.rebuild_button.clicked.connect(self._rebuild_embeddings); detail_layout.addWidget(self.rebuild_button)
        form = QFormLayout(); form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        self.content_edit = QTextEdit(detail); self.content_edit.setMinimumHeight(110)
        self.scope_combo = QComboBox(detail); self.scope_combo.currentIndexChanged.connect(self._scope_changed)
        self.target_combo = QComboBox(detail)
        self.weight_spin = QDoubleSpinBox(detail); self.weight_spin.setRange(0.0, 1.0); self.weight_spin.setSingleStep(0.05); self.weight_spin.setDecimals(2); self.weight_spin.setValue(0.5)
        self.enabled_check = QCheckBox("Enabled", detail); self.enabled_check.setChecked(True)
        self.target_label = QLabel("Target:", detail)
        form.addRow("Memory content:", self.content_edit); form.addRow("Scope:", self.scope_combo)
        form.addRow(self.target_label, self.target_combo); form.addRow("Weight:", self.weight_spin); form.addRow("", self.enabled_check)
        detail_layout.addLayout(form); detail_layout.addStretch(1); root.addWidget(detail, 3)
        self._configure_role(); self._load_memories(); self._new_memory()

    def _owner_id(self) -> int:
        return int(self._session.current.user_id)

    def _configure_role(self) -> None:
        self.scope_combo.clear()
        if str(self._session.current.role).lower() == "admin":
            self.scope_combo.addItem("Admin-wide", "admin")
            self.scope_combo.addItem("Global — all users", "global")
        else:
            self.scope_combo.addItem("User-wide", "user")
            self.scope_combo.addItem("Specific chat…", "chat")
            self.scope_combo.addItem("Specific AI profile…", "profile")

    def _scope_changed(self) -> None:
        if self._loading: return
        scope = self.scope_combo.currentData()
        targeted = scope in {"chat", "profile"}
        self.target_label.setVisible(targeted); self.target_combo.setVisible(targeted)
        self.target_combo.clear()
        if scope == "chat":
            for chat in db_ops.list_conversations(self._conn, self._owner_id(), limit=10000):
                self.target_combo.addItem(chat.get("title") or f"Chat {chat['id']}", int(chat["id"]))
        elif scope == "profile":
            for profile in db_ops.list_ai_profiles(self._conn, owner_user_id=self._owner_id(), include_builtin=True):
                self.target_combo.addItem(profile.get("display_name") or f"Profile {profile['id']}", int(profile["id"]))

    def _load_memories(self, select_id: int | None = None) -> None:
        self.memory_list.clear()
        try:
            memories = db_ops.list_memories(self._conn, owner_user_id=self._owner_id())
        except Exception as exc:
            self.status.setText(str(exc)); return
        for memory in memories:
            preview = " ".join((memory["content"] or "").split())[:72] or "(empty)"
            item = self.memory_list.addItem(f"[{memory['scope']}] {preview}")
            row = self.memory_list.item(self.memory_list.count() - 1); row.setData(Qt.ItemDataRole.UserRole, memory["id"])
            if memory["id"] == select_id: self.memory_list.setCurrentItem(row)
        if not memories: self.status.setText("No memories yet. Create one manually; nothing is sent to a model in this phase.")

    def _new_memory(self) -> None:
        self._loading = True; self._memory_id = None; self.memory_list.clearSelection(); self.content_edit.clear(); self.weight_spin.setValue(0.5); self.enabled_check.setChecked(True); self._loading = False
        self._scope_changed(); self.delete_button.setEnabled(False); self.status.setText("New memory — save to persist it.")

    def _select_memory(self, current, previous) -> None:
        if not current: return
        try: memory = db_ops.get_memory(self._conn, owner_user_id=self._owner_id(), memory_id=int(current.data(Qt.ItemDataRole.UserRole)))
        except Exception as exc: self.status.setText(str(exc)); return
        if not memory: return
        self._loading = True; self._memory_id = int(memory["id"]); self.content_edit.setPlainText(memory["content"] or "")
        idx = self.scope_combo.findData(memory["scope"]); self.scope_combo.setCurrentIndex(idx); self._loading = False; self._scope_changed()
        target = memory["conversation_id"] if memory["scope"] == "chat" else memory["profile_id"]
        if target is not None:
            target_idx = self.target_combo.findData(int(target)); self.target_combo.setCurrentIndex(target_idx)
        self.weight_spin.setValue(float(memory["weight"])); self.enabled_check.setChecked(bool(memory["enabled"])); self.delete_button.setEnabled(True); self.status.setText("")

    def _save_memory(self) -> None:
        scope = self.scope_combo.currentData(); target = self.target_combo.currentData()
        kwargs = dict(owner_user_id=self._owner_id(), content=self.content_edit.toPlainText(), scope=scope,
                      conversation_id=target if scope == "chat" else None, profile_id=target if scope == "profile" else None,
                      weight=self.weight_spin.value(), enabled=self.enabled_check.isChecked())
        try:
            if self._memory_id is None: selected = db_ops.create_memory(self._conn, **kwargs)
            else: db_ops.update_memory(self._conn, memory_id=self._memory_id, **kwargs); selected = self._memory_id
            self._load_memories(select_id=selected); self.status.setText("Memory saved; preparing embedding."); self._queue_embedding(selected)
        except Exception as exc:
            QMessageBox.warning(self, "Memory not saved", str(exc))

    def _delete_memory(self) -> None:
        if self._memory_id is None: return
        if QMessageBox.question(self, "Delete memory", "Delete this memory?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes: return
        try:
            db_ops.delete_memory(self._conn, owner_user_id=self._owner_id(), memory_id=self._memory_id)
            self._load_memories(); self._new_memory(); self.status.setText("Memory deleted.")
        except Exception as exc:
            QMessageBox.warning(self, "Memory not deleted", str(exc))

    def _queue_embedding(self, memory_id: int) -> None:
        if self._embedding_thread is not None: return
        prepared = self._embedding_service.embedding_input(memory_id)
        if prepared is None:
            self.embedding_status.setText("Embeddings: not required or disabled"); return
        mid, content, fingerprint = prepared
        job_id = next(_JOB_IDS)
        thread = QThread()  # deliberately unparented; registry retains it after panel closure
        worker = _EmbeddingWorker(job_id, self._embedding_generation, mid, content, fingerprint, self._embedding_service.provider)
        _ACTIVE_EMBEDDING_JOBS[job_id] = (thread, worker)
        self._embedding_thread = thread
        worker.moveToThread(thread); thread.started.connect(worker.run); worker.done.connect(self._embedding_done)
        worker.done.connect(thread.quit); worker.done.connect(worker.deleteLater)
        thread.finished.connect(lambda job_id=job_id: _finish_job(job_id))
        thread.finished.connect(thread.deleteLater)
        log.info("Embedding job created id=%s memory_id=%s generation=%s", job_id, mid, self._embedding_generation)
        thread.start(); self.embedding_status.setText("Embeddings: generating…")

    def _embedding_done(self, job_id, generation, memory_id, fingerprint, vector, error):
        if generation != self._embedding_generation:
            log.debug("Embedding result rejected stale id=%s memory_id=%s", job_id, memory_id)
            return
        self._embedding_thread = None
        if error: status = "failed"; self.embedding_status.setText("Embeddings: unavailable; memory remains saved")
        else:
            try: status = self._embedding_service.store_generated_vector(memory_id, fingerprint, vector)
            except Exception: status = "failed"
        self.embedding_status.setText(f"Embeddings: {status}")
        if self._rebuild_active:
            if status.startswith("embedded"): self._rebuild_done += 1
            else: self._rebuild_failed += 1
            self._rebuild_next()

    def _rebuild_embeddings(self) -> None:
        if self._rebuild_active or self._embedding_thread is not None: self.status.setText("Embedding work is already running."); return
        rows = self._conn.execute("SELECT id FROM persistent_memory WHERE owner_user_id=? AND enabled=1 AND scope!='global' ORDER BY id", (self._owner_id(),)).fetchall()
        if not rows: self.embedding_status.setText("Embeddings: nothing to rebuild"); return
        self._rebuild_ids = [int(row[0]) for row in rows]; self._rebuild_done = self._rebuild_failed = 0; self._rebuild_active = True; self._rebuild_next()

    def _rebuild_next(self):
        if not self._rebuild_ids:
            self._rebuild_active = False
            self.embedding_status.setText(f"Embeddings rebuilt: {self._rebuild_done} ready, {self._rebuild_failed} failed"); return
        memory_id = self._rebuild_ids.pop(0); self._queue_embedding(memory_id)

    def _invalidate_embedding_work(self, *args) -> None:
        self._embedding_generation += 1
        self._rebuild_active = False
        self._rebuild_ids = []
        self._embedding_service.clear_strict_vectors()
        self._embedding_thread = None
        log.info("Memory manager invalidated embedding work generation=%s", self._embedding_generation)

    def _close(self) -> None:
        self._invalidate_embedding_work()
        self.sig_close.emit()
