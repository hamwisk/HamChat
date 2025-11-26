# hamchat/infra/llm/thread_broker.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable, Deque, Iterator, Optional
from collections import deque
from itertools import count

from PyQt6.QtCore import QObject, QThread, pyqtSignal, Qt


# ---------- Public types ----------
StreamFunc = Callable[..., Iterator[str]]
StopFn     = Callable[[], bool]

@dataclass(slots=True)
class Job:
    ticket: int
    func:   StreamFunc
    args:   tuple = field(default_factory=tuple)
    kwargs: dict  = field(default_factory=dict)


# ---------- Worker ----------
class _Worker(QObject):
    token    = pyqtSignal(int, str)     # (ticket, chunk)
    finished = pyqtSignal(int, str)     # (ticket, status) status: "ok"|"cancelled"|"error"
    error    = pyqtSignal(int, str)     # (ticket, message)

    def __init__(self, job: Job):
        super().__init__()
        self._job = job
        self._should_stop = False
        self._iter: Optional[Iterator[str]] = None

    def stop(self):  # thread-safe (Qt will marshal to our thread via QueuedConnection)
        self._should_stop = True
        it = self._iter
        try:
            if it and hasattr(it, "close"):
                it.close()
        except Exception:
            pass

    def _stop_fn(self) -> bool:
        return self._should_stop

    def run(self):
        ticket = self._job.ticket
        status = "ok"
        try:
            # Inject cooperative cancel hook if the func accepts it.
            kw = dict(self._job.kwargs)
            kw.setdefault("stop_fn", self._stop_fn)

            result = self._job.func(*self._job.args, **kw)

            if result is not None:
                self._iter = iter(result)
                for chunk in self._iter:
                    if self._should_stop:
                        status = "cancelled"
                        break
                    self.token.emit(ticket, str(chunk))
        except Exception as exc:
            status = "error"
            self.error.emit(ticket, f"{type(exc).__name__}: {exc}")
        finally:
            try:
                if self._iter and hasattr(self._iter, "close"):
                    self._iter.close()
            except Exception:
                pass
            self._iter = None
            self.finished.emit(ticket, status)


# ---------- Broker ----------
class ThreadBroker(QObject):
    """
    Single-concurrency ticket queue for streaming LLM work.
    Submit a callable that yields text chunks. Cooperative stop supported.
    """
    job_started  = pyqtSignal(int)          # ticket
    job_token    = pyqtSignal(int, str)     # (ticket, chunk)
    job_finished = pyqtSignal(int, str)     # (ticket, status)
    job_error    = pyqtSignal(int, str)     # (ticket, message)
    queue_changed = pyqtSignal(int, int)    # (active_ticket or -1, queued_count)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._tickets = count(1)
        self._queue: Deque[Job] = deque()
        self._thread: Optional[QThread] = None
        self._worker: Optional[_Worker] = None
        self._active_ticket: int = -1

    # -------- API --------
    def submit(self, func: StreamFunc, *args, **kwargs) -> int:
        ticket = next(self._tickets)
        self._queue.append(Job(ticket, func, args, kwargs))
        self.queue_changed.emit(self._active_ticket, len(self._queue))
        if self._active_ticket == -1:
            self._start_next()
        return ticket

    def stop_active(self):
        # gentle, cooperative cancel
        if self._worker:
            self._worker.stop()

    def cancel_ticket(self, ticket: int):
        # remove pending job if queued; if it’s active, treat as stop
        if ticket == self._active_ticket:
            self.stop_active()
            return
        self._queue = deque(j for j in self._queue if j.ticket != ticket)
        self.queue_changed.emit(self._active_ticket, len(self._queue))

    def clear_queue(self, include_active: bool = False):
        self._queue.clear()
        if include_active:
            self.stop_active()
        self.queue_changed.emit(self._active_ticket, 0)

    def active_ticket(self) -> int:
        return self._active_ticket

    # -------- internals --------
    def _start_next(self):
        if self._thread or self._worker or not self._queue:
            return

        job = self._queue.popleft()
        self._active_ticket = job.ticket
        self.queue_changed.emit(self._active_ticket, len(self._queue))

        self._thread = QThread()
        self._worker = _Worker(job)
        self._worker.moveToThread(self._thread)

        # bubble up signals
        self._worker.token.connect(self.job_token, Qt.ConnectionType.QueuedConnection)
        self._worker.finished.connect(self._on_worker_finished, Qt.ConnectionType.QueuedConnection)
        self._worker.error.connect(self.job_error, Qt.ConnectionType.QueuedConnection)

        self._thread.started.connect(self._worker.run)
        self._thread.start()
        self.job_started.emit(job.ticket)

    def _cleanup(self):
        if self._thread:
            self._thread.quit()
            self._thread.wait()
            self._thread.deleteLater()
            self._thread = None
        if self._worker:
            self._worker.deleteLater()
            self._worker = None
        self._active_ticket = -1

    def _on_worker_finished(self, ticket: int, status: str):
        self.job_finished.emit(ticket, status)
        self._cleanup()
        self._start_next()
