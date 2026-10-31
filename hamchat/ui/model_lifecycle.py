"""Asynchronous model menu operations; no conversation or prompt construction."""
from PyQt6.QtCore import QObject, QThread, pyqtSignal


# Retain workers until their threads exit, even if their window is closed.
_JOBS = set()


class LifecycleWorker(QObject):
    done = pyqtSignal(str)

    def __init__(self, operation, client, model, options):
        super().__init__()
        self.operation, self.client = operation, client
        self.model, self.options = model, options

    def run(self):
        try:
            if self.operation == "unload":
                result = self.client.unload_all_models()
                if result.error or result.failed or result.skipped:
                    raise RuntimeError(result.error or f"failed {len(result.failed)}, skipped {len(result.skipped)}")
                message = f"Unloaded {len(result.unloaded)} model(s)."
            else:
                context = self.client.prepare_runtime_context(
                    model=self.model, options=self.options, force=True,
                )
                if context.source == "fallback":
                    raise RuntimeError("Could not confirm model preparation; check backend availability and model support.")
                message = f"Loaded and warmed {self.model}."
        except Exception as exc:
            message = f"Model operation failed: {exc}"
        self.done.emit(message)


class ModelLifecycle(QObject):
    status = pyqtSignal(str)

    def __init__(self, session, controller, parent=None):
        super().__init__(parent)
        self.session, self.controller = session, controller

    def start(self, operation):
        if self.session.model_lifecycle_busy or self.controller.broker.active_ticket() != -1:
            self.status.emit("Stop or wait for the current model operation or response.")
            return
        client = self.controller._model_client
        model = self.controller._model_name
        # Never route an external backend to Ollama as a lifecycle fallback.
        from hamchat.infra.llm.ollama_client import OllamaClient
        if not self.session.is_ollama_model(model) or not isinstance(client, OllamaClient):
            self.status.emit("Model lifecycle operations are unsupported by the selected backend.")
            return
        options = {}
        allocation = self.session.get_model_context_num_ctx(model)
        if allocation is not None:
            options["num_ctx"] = allocation
        self.session.set_model_lifecycle_busy(True)
        self.status.emit("Unloading all models…" if operation == "unload" else f"Loading and warming {model}…")
        thread = QThread()
        worker = LifecycleWorker(operation, client, model, options)
        pair = (thread, worker)
        _JOBS.add(pair)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._finished)
        worker.done.connect(thread.quit)
        worker.done.connect(worker.deleteLater)
        thread.finished.connect(lambda: _JOBS.discard(pair))
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _finished(self, message):
        self.session.set_model_lifecycle_busy(False)
        self.status.emit(message)
