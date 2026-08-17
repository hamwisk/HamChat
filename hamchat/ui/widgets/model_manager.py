# hamchat/ui/widgets/model_manager.py

from __future__ import annotations

import logging
import itertools
from typing import Optional, List, Tuple, Dict, Any

from PyQt6.QtCore import (
    Qt,
    QSortFilterProxyModel,
    QAbstractTableModel,
    QModelIndex,
    QVariant,
    QObject,
    QThread,
    pyqtSignal,
)
from PyQt6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QLabel,
    QPushButton,
    QLineEdit,
    QComboBox,
    QCheckBox,
    QTableView,
    QHeaderView,
)

logger = logging.getLogger(__name__)
_UNLOAD_JOB_IDS = itertools.count(1)
_ACTIVE_UNLOAD_JOBS = {}


class _UnloadWorker(QObject):
    done = pyqtSignal(int, object)

    def __init__(self, job_id, client, capabilities_for):
        super().__init__()
        self.job_id = job_id
        self.client = client
        self.capabilities_for = capabilities_for

    def run(self) -> None:
        try:
            result = self.client.unload_all_models(capabilities_for=self.capabilities_for)
        except Exception as exc:
            logger.exception("Unexpected Ollama unload worker failure")
            result = None
            error = str(exc)
        else:
            error = ""
        self.done.emit(self.job_id, (result, error))


# ---------------------------------------------------------------------------
# Table model for models list
# ---------------------------------------------------------------------------

class ModelsTableModel(QAbstractTableModel):
    """
    Simple table model over a list of model dicts.

    Each row dict should have:
      - id: str         (model_id)
      - label: str      (display label)
      - backend: str    ('ollama', 'openai', 'other', or '')
      - context: int|None
      - vision: bool
    """

    COLUMNS = ["Name", "Backend", "Context", "Vision"]

    def __init__(self, rows: Optional[List[Dict[str, Any]]] = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._rows: List[Dict[str, Any]] = rows or []

    # API to reset rows
    def set_rows(self, rows: List[Dict[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self.COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            if 0 <= section < len(self.COLUMNS):
                return self.COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        col = index.column()
        if not (0 <= row < len(self._rows)):
            return None
        item = self._rows[row]

        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return item.get("label") or item.get("id")
            elif col == 1:
                backend = item.get("backend") or ""
                if not backend:
                    return "ollama"
                return backend
            elif col == 2:
                ctx = item.get("context")
                return "" if ctx is None else str(ctx)
            elif col == 3:
                return "✔" if item.get("vision") else "✖"

        if role == Qt.ItemDataRole.UserRole:
            # store the model_id on the first column
            if col == 0:
                return item.get("id")

        return None

    def get_row(self, row: int) -> Optional[Dict[str, Any]]:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None


# ---------------------------------------------------------------------------
# Proxy for filter/sort
# ---------------------------------------------------------------------------

class ModelsFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._search: str = ""
        self._backend: str = "all"  # 'all' | 'ollama' | 'openai' | 'other'
        self._vision_only: bool = False

    def set_search(self, text: str) -> None:
        self._search = (text or "").strip().lower()
        self.invalidateFilter()

    def set_backend_filter(self, backend: str) -> None:
        backend = (backend or "all").lower()
        if backend not in ("all", "ollama", "openai", "other"):
            backend = "all"
        self._backend = backend
        self.invalidateFilter()

    def set_vision_only(self, on: bool) -> None:
        self._vision_only = bool(on)
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        src = self.sourceModel()
        if not isinstance(src, ModelsTableModel):
            return True

        row = src.get_row(source_row)
        if not row:
            return True

        name = (row.get("label") or row.get("id") or "").lower()
        backend = (row.get("backend") or "ollama").lower()
        vision = bool(row.get("vision"))

        # Vision-only filter
        if self._vision_only and not vision:
            return False

        # Backend filter
        if self._backend != "all":
            if self._backend == "ollama":
                if backend not in ("", "ollama"):
                    return False
            elif self._backend == "openai":
                if backend != "openai":
                    return False
            elif self._backend == "other":
                if backend in ("", "ollama", "openai"):
                    return False

        # Text search
        if self._search:
            if self._search not in name:
                return False

        return True


# ---------------------------------------------------------------------------
# Main widget
# ---------------------------------------------------------------------------

class ModelManager(QWidget):
    """
    Top-panel widget for managing / selecting models.

    - Left: filter controls + sortable table of models
    - Right: details + Activate button

    Emits:
      - sig_close: when the Close button is pressed
      - sig_model_activated(str): when a model is chosen (double-click or button)
    """

    sig_close = pyqtSignal()
    sig_model_activated = pyqtSignal(str)

    def __init__(
        self,
        session_mgr,
        parent: Optional[QWidget] = None,
        *,
        is_generation_active=None,
        ollama_client=None,
    ) -> None:
        super().__init__(parent)
        self._session = session_mgr
        self._is_generation_active = is_generation_active or (lambda: False)
        self._ollama_client = ollama_client
        self._unload_busy = False

        self._table_model = ModelsTableModel()
        self._proxy = ModelsFilterProxy(self)
        self._proxy.setSourceModel(self._table_model)

        self._build_ui()
        self._load_models()

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(12)

        # Left: filters + table
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(6)

        # Header row: title + close button
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)

        title = QLabel("Model Manager", self)
        title.setStyleSheet("font-weight: bold;")
        btn_close = QPushButton("Close", self)
        btn_close.clicked.connect(self.sig_close.emit)

        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(btn_close)

        # Filter row
        filter_row = QHBoxLayout()
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.setSpacing(6)

        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText("Search models…")

        self.backend_combo = QComboBox(self)
        self.backend_combo.addItem("All backends", "all")
        self.backend_combo.addItem("Ollama", "ollama")
        self.backend_combo.addItem("OpenAI", "openai")
        self.backend_combo.addItem("Other", "other")

        self.vision_only_chk = QCheckBox("Vision only", self)

        filter_row.addWidget(self.search_edit, 1)
        filter_row.addWidget(self.backend_combo)
        filter_row.addWidget(self.vision_only_chk)

        # Table
        self.table = QTableView(self)
        self.table.setModel(self._proxy)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.doubleClicked.connect(self._on_table_activated)

        left.addLayout(header)
        left.addLayout(filter_row)
        left.addWidget(self.table)

        # Right: details + activate
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(6)

        self.detail_name = QLabel("Name: —", self)
        self.detail_backend = QLabel("Backend: —", self)
        self.detail_context = QLabel("Context: —", self)
        self.detail_caps = QLabel("Capabilities: —", self)
        self.unload_status = QLabel("", self)
        self.unload_status.setWordWrap(True)
        self.context_allocation = QComboBox(self)
        self.context_allocation.addItem("Auto — Ollama default", "auto")
        self.context_allocation.addItem("Low — 4K", "low")
        self.context_allocation.addItem("Mid — 8K", "mid")
        self.context_allocation.addItem("High — 16K", "high")

        right.addWidget(self.detail_name)
        right.addWidget(self.detail_backend)
        right.addWidget(self.detail_context)
        right.addWidget(self.detail_caps)
        right.addWidget(self.unload_status)
        right.addWidget(QLabel("Runtime context allocation:", self))
        right.addWidget(self.context_allocation)
        right.addStretch(1)

        self.btn_activate = QPushButton("Activate model", self)
        self.btn_activate.clicked.connect(self._on_activate_clicked)
        self.btn_unload_all = QPushButton("Unload all models", self)
        self.btn_unload_all.setAccessibleName("Unload all models")
        self.btn_unload_all.setToolTip("Free Ollama RAM/VRAM. Models reload automatically when next used.")
        self.btn_unload_all.clicked.connect(self._on_unload_all_clicked)
        actions = QHBoxLayout()
        actions.addWidget(self.btn_unload_all)
        actions.addWidget(self.btn_activate)
        right.addLayout(actions)

        root.addLayout(left, 3)
        root.addLayout(right, 2)

        # Wire filters
        self.search_edit.textChanged.connect(self._proxy.set_search)
        self.backend_combo.currentIndexChanged.connect(self._on_backend_changed)
        self.vision_only_chk.toggled.connect(self._proxy.set_vision_only)
        self.table.selectionModel().currentChanged.connect(self._on_selection_changed)
        self.context_allocation.currentIndexChanged.connect(self._on_context_allocation_changed)

    # ------------------------------------------------------------------ #
    # Data loading
    # ------------------------------------------------------------------ #

    def _load_models(self) -> None:
        """
        Populate table from SessionManager get_model_choices + capability helpers.
        """
        rows: List[Dict[str, Any]] = []
        choices: List[Tuple[str, str]] = []

        try:
            if hasattr(self._session, "get_model_choices"):
                choices = self._session.get_model_choices()
        except Exception as exc:
            logger.error("ModelManager: failed to get model choices: %s", exc)
            choices = []

        for model_id, label in choices or []:
            backend = ""
            caps: Dict[str, Any] = {}
            ctx: Optional[int] = None

            # Backend
            try:
                if hasattr(self._session, "get_model_backend"):
                    backend = self._session.get_model_backend(model_id) or ""
            except Exception:
                backend = ""

            # Capabilities
            try:
                if hasattr(self._session, "get_model_capabilities"):
                    caps = self._session.get_model_capabilities(model_id) or {}
            except Exception:
                caps = {}

            # Context
            try:
                if hasattr(self._session, "get_model_context"):
                    ctx = self._session.get_model_context(model_id)
            except Exception:
                ctx = None

            vision = bool(caps.get("vision"))

            rows.append(
                {
                    "id": model_id,
                    "label": label or model_id,
                    "backend": backend or "",  # defaulted to ollama in display
                    "context": ctx,
                    "vision": vision,
                    "caps": caps,
                }
            )

        self._table_model.set_rows(rows)
        if rows:
            # Select first row by default
            first_index = self._proxy.index(0, 0)
            if first_index.isValid():
                self.table.selectRow(first_index.row())
                self._update_details_from_index(first_index)
        else:
            self._clear_details()

    # ------------------------------------------------------------------ #
    # Filter / selection helpers
    # ------------------------------------------------------------------ #

    def _on_backend_changed(self, idx: int) -> None:
        backend = self.backend_combo.currentData()
        self._proxy.set_backend_filter(backend)

    def _on_selection_changed(self, current: QModelIndex, previous: QModelIndex) -> None:
        if current.isValid():
            self._update_details_from_index(current)
        else:
            self._clear_details()

    def _update_details_from_index(self, proxy_index: QModelIndex) -> None:
        src_index = self._proxy.mapToSource(proxy_index)
        row = self._table_model.get_row(src_index.row())
        if not row:
            self._clear_details()
            return

        model_id = row.get("id") or "?"
        name = row.get("label") or model_id
        backend = row.get("backend") or "ollama"
        ctx = row.get("context")
        caps = row.get("caps") or {}
        vision = "vision" if caps.get("vision") else ""
        # If you later add tools / function calling flags, include them here:
        cap_bits = [b for b in [vision] if b]
        cap_str = ", ".join(cap_bits) if cap_bits else "—"

        self.detail_name.setText(f"Name: {name}")
        self.detail_backend.setText(f"Backend: {backend}")
        self.detail_context.setText(f"Context: {ctx if ctx is not None else 'unknown'}")
        self.detail_caps.setText(f"Capabilities: {cap_str}")
        is_ollama = not hasattr(self._session, "is_ollama_model") or self._session.is_ollama_model(model_id)
        self.context_allocation.setEnabled(is_ollama)
        if is_ollama:
            mode = self._session.get_model_context_allocation(model_id)
            combo_index = self.context_allocation.findData(mode)
            self.context_allocation.blockSignals(True)
            self.context_allocation.setCurrentIndex(max(0, combo_index))
            self.context_allocation.blockSignals(False)
        else:
            self.context_allocation.setCurrentIndex(0)

    def _clear_details(self) -> None:
        self.detail_name.setText("Name: —")
        self.detail_backend.setText("Backend: —")
        self.detail_context.setText("Context: —")
        self.detail_caps.setText("Capabilities: —")
        self.context_allocation.setEnabled(False)
        self.context_allocation.setCurrentIndex(0)

    def _on_context_allocation_changed(self, _index: int) -> None:
        model_id = self._get_current_model_id()
        if not model_id or not hasattr(self._session, "set_model_context_allocation"):
            return
        self._session.set_model_context_allocation(model_id, self.context_allocation.currentData())

    # ------------------------------------------------------------------ #
    # Activation
    # ------------------------------------------------------------------ #

    def _get_current_model_id(self) -> Optional[str]:
        selection = self.table.selectionModel()
        if not selection:
            return None
        indexes = selection.selectedRows()
        if not indexes:
            return None
        proxy_index = indexes[0]
        src_index = self._proxy.mapToSource(proxy_index)
        row = self._table_model.get_row(src_index.row())
        if not row:
            return None
        return row.get("id")

    def _on_table_activated(self, proxy_index: QModelIndex) -> None:
        model_id = self._get_current_model_id()
        if model_id:
            self.sig_model_activated.emit(model_id)

    def _on_activate_clicked(self) -> None:
        model_id = self._get_current_model_id()
        if model_id:
            self.sig_model_activated.emit(model_id)

    def _on_unload_all_clicked(self) -> None:
        if self._unload_busy:
            return
        try:
            active = bool(self._is_generation_active())
        except Exception:
            active = True
        if active:
            self.unload_status.setText("Stop or wait for the current response before unloading models.")
            return

        from hamchat.infra.llm.ollama_client import OllamaClient
        self._unload_busy = True
        self.btn_unload_all.setEnabled(False)
        self.unload_status.setText("Unloading Ollama models…")
        client = self._ollama_client or OllamaClient()
        job_id = next(_UNLOAD_JOB_IDS)
        thread = QThread()
        worker = _UnloadWorker(job_id, client, self._known_ollama_capabilities)
        _ACTIVE_UNLOAD_JOBS[job_id] = (thread, worker)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_unload_finished)
        worker.done.connect(thread.quit)
        worker.done.connect(worker.deleteLater)
        thread.finished.connect(lambda: _ACTIVE_UNLOAD_JOBS.pop(job_id, None))
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _known_ollama_capabilities(self, model_id: str):
        try:
            metadata = self._session.get_model_metadata(model_id)
            capabilities = metadata.get("ollama_capabilities")
            return capabilities if isinstance(capabilities, list) else None
        except Exception:
            return None

    def _on_unload_finished(self, _job_id: int, outcome: object) -> None:
        self._unload_busy = False
        self.btn_unload_all.setEnabled(True)
        result, error = outcome
        if result is None:
            self.unload_status.setText(f"Could not unload Ollama models: {error or 'unknown error'}")
            return
        if result.error and result.loaded_count == 0:
            self.unload_status.setText(f"Could not query Ollama models: {result.error}")
            return
        if result.loaded_count == 0:
            self.unload_status.setText("No Ollama models are currently loaded.")
            return
        if result.error:
            self.unload_status.setText(
                f"Unloaded {len(result.unloaded)} model(s); failed {len(result.failed)}; "
                f"skipped {len(result.skipped)}. Could not verify remaining models: {result.error}"
            )
            return
        if not result.failed and not result.skipped:
            self.unload_status.setText(f"Unloaded {len(result.unloaded)} model(s).")
            return
        self.unload_status.setText(
            f"Unloaded {len(result.unloaded)} model(s); failed {len(result.failed)}; "
            f"skipped {len(result.skipped)}."
        )

    # ------------------------------------------------------------------ #
    # Convenience for external callers
    # ------------------------------------------------------------------ #

    def apply_vision_filter(self) -> None:
        """
        Helper for the 'current model is blind but message has image' flow.
        Call this before showing the panel to pre-filter to vision models.
        """
        self.vision_only_chk.setChecked(True)
