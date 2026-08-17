"""Qt orchestration for the already verified system-only updater backend.

This module deliberately contains no archive, staging, digest, journal, or
installation policy.  Those operations stay in :mod:`updates`,
:mod:`update_acquisition`, and :mod:`system_update_executor`.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import logging
from pathlib import Path
import subprocess
import sys
import time
import uuid
from typing import Any, Callable

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QMessageBox

from .constants import __version__
from .settings import load_settings
from .system_update_executor import SystemUpdateStatus, install_verified_candidate
from .update_acquisition import AcquisitionRequest, acquire_verified_candidate
from .updates import (
    DecisionReason, RemoteCheckStatus, SemanticVersion, UpdateMode,
    UpdatePreferences, UrllibTransport, check_for_updates,
    preferences_from_settings, release_manifest_digest, save_update_preferences,
)


log = logging.getLogger("updates")
_NOTES_LIMIT = 12_000


def transaction_parent_for_installation(installation_root: Path) -> Path:
    """Stable private parent used only for system-update transaction evidence."""
    import tempfile
    key = hashlib.sha256(str(Path(installation_root).resolve()).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "hamchat-updates" / key


class UpdateController(QObject):
    """One owner for startup/manual checks and the UI-facing decision flow."""

    startup_finished = pyqtSignal(bool)  # True means old process must stop.
    operation_changed = pyqtSignal(bool)
    _invoke_gui = pyqtSignal(object)

    def __init__(
        self,
        *,
        settings_path: Path,
        installation_root: Path,
        data_root: Path,
        splash: Any | None = None,
        parent: QObject | None = None,
        transport_factory: Callable[[], Any] = UrllibTransport,
        check_function: Callable[..., Any] = check_for_updates,
        acquire_function: Callable[..., Any] = acquire_verified_candidate,
        install_function: Callable[..., Any] = install_verified_candidate,
        restart_function: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings_path = Path(settings_path)
        self._installation_root = Path(installation_root).resolve()
        self._data_root = Path(data_root).resolve()
        self._splash = splash
        self._transport_factory = transport_factory
        self._check = check_function
        self._acquire = acquire_function
        self._install = install_function
        self._restart = restart_function or self._launch_replacement
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hamchat-updates")
        self._active = False
        self._startup = False
        self._stopping = False
        self._window = None
        self._invoke_gui.connect(self._run_gui_callback)
        self._reload_preferences()
        log.debug("Update controller created")

    def attach_window(self, window: Any) -> None:
        self._window = window

    def _reload_preferences(self) -> None:
        settings = load_settings(self._settings_path)
        try:
            self._preferences = preferences_from_settings(settings)
        except Exception:
            self._preferences = UpdatePreferences()
            log.warning("Malformed update-mode setting ignored; using Ask")

    def mode_value(self) -> str:
        return self._preferences.mode.value

    def set_mode(self, value: str) -> None:
        try:
            mode = UpdateMode("ask" if value == "prompt" else value)
        except (TypeError, ValueError):
            log.warning("Malformed update-mode setting ignored; using Ask")
            mode = UpdateMode.PROMPT
        self._preferences = UpdatePreferences(mode, self._preferences.ignore_patch_updates, self._preferences.skipped_version)
        settings = load_settings(self._settings_path)
        save_update_preferences(self._settings_path, settings, self._preferences)
        log.info("Update mode selected mode=%s", mode.value)

    def _save_skip(self, version: str | None) -> None:
        self._preferences = UpdatePreferences(self._preferences.mode, self._preferences.ignore_patch_updates, version)
        save_update_preferences(self._settings_path, load_settings(self._settings_path), self._preferences)

    def start_startup_check(self) -> None:
        if self._preferences.mode is UpdateMode.OFF:
            log.info("Startup update checking disabled mode=off")
            self.startup_finished.emit(False)
            return
        self._begin_check(startup=True, manual=False)

    def check_manually(self) -> None:
        self._begin_check(startup=False, manual=True)

    def _begin_check(self, *, startup: bool, manual: bool) -> None:
        if self._active or self._stopping:
            log.debug("Duplicate update operation refused")
            return
        self._active, self._startup = True, startup
        self.operation_changed.emit(True)
        self._status("Checking for HamChat updates…")
        log.info("%s update check started", "Startup" if startup else "Manual")
        future = self._pool.submit(self._check, __version__, self._preferences, manual_check=manual, transport=self._transport_factory())
        future.add_done_callback(lambda task: self._deliver(lambda: self._checked(task, startup, manual)))

    def _deliver(self, callback: Callable[[], None]) -> None:
        # Future callbacks run on worker threads.  A queued Qt signal moves
        # result handling, widgets, and dialogs back to this controller's GUI
        # thread without depending on a worker-thread event loop.
        self._invoke_gui.emit(callback)

    def _run_gui_callback(self, callback: Callable[[], None]) -> None:
        if not self._stopping:
            callback()

    def _checked(self, task: Any, startup: bool, manual: bool) -> None:
        if self._stopping:
            return
        try:
            result = task.result()
        except Exception as exc:
            log.error("Update check failed exception_type=%s", type(exc).__name__)
            self._finish_check(startup, manual, "Unable to check for updates.")
            return
        log.info("Update manifest check completed status=%s decision=%s", result.status.value, result.decision.reason.value)
        if result.decision.reason is DecisionReason.UPDATE_AVAILABLE and result.manifest is not None:
            log.info("Eligible update discovered version=%s", result.manifest.version)
            self._handle_candidate(result, startup, manual)
            return
        if result.decision.reason is DecisionReason.VERSION_SKIPPED:
            log.warning("Exact skipped update suppressed")
        elif result.decision.reason is DecisionReason.DATA_COMPATIBILITY_BLOCKED:
            log.warning("Update blocked by data compatibility")
        elif result.status is RemoteCheckStatus.NO_ELIGIBLE_UPDATE:
            log.info("No eligible update")
        if manual:
            message = f"HamChat {__version__} is already up to date." if result.status is RemoteCheckStatus.NO_ELIGIBLE_UPDATE else "Update check could not use this release."
            self._message(QMessageBox.Icon.Information if result.status is RemoteCheckStatus.NO_ELIGIBLE_UPDATE else QMessageBox.Icon.Warning, "HamChat updates", message)
        self._finish_check(startup, manual)

    def _handle_candidate(self, result: Any, startup: bool, manual: bool) -> None:
        manifest = result.manifest
        assert manifest is not None
        automatic = startup and self._preferences.mode is UpdateMode.AUTOMATIC
        if automatic:
            self._begin_install(result, startup=True)
            return
        self._close_splash()
        self._activate_window()
        notes = (result.release_notes or "Release notes are unavailable.")[:_NOTES_LIMIT]
        box = QMessageBox(self._window)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("HamChat update available")
        box.setText(f"HamChat {manifest.version} is available.")
        box.setDetailedText(notes)
        install = box.addButton("Install Now", QMessageBox.ButtonRole.AcceptRole)
        skip = box.addButton(f"Skip {manifest.version}", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Not Now", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is install:
            log.info("User selected Install Now version=%s", manifest.version)
            if self._preferences.skipped_version == str(manifest.version):
                self._save_skip(None)
            self._begin_install(result, startup=startup)
        elif box.clickedButton() is skip:
            log.info("User selected Skip version=%s", manifest.version)
            self._save_skip(str(manifest.version))
            self._finish_check(startup, manual)
        else:
            log.info("User selected Not Now version=%s", manifest.version)
            self._finish_check(startup, manual)

    def _transaction_root(self) -> Path:
        # Transaction material is intentionally not stored in the data root or
        # release checkout.  This stable, user-private temp location is only
        # used by the system-file transaction backend.
        return transaction_parent_for_installation(self._installation_root) / f"tx-{uuid.uuid4().hex}"

    def _begin_install(self, result: Any, *, startup: bool) -> None:
        manifest = result.manifest
        assert manifest is not None
        self._status(f"Downloading HamChat {manifest.version}…")
        root = self._transaction_root()
        tx_id = root.name
        request = AcquisitionRequest(
            decision=result.decision, manifest_digest=release_manifest_digest(manifest),
            installation_root=self._installation_root, data_root=self._data_root,
            transaction_root=root, transaction_id=tx_id,
        )
        log.info("Candidate acquisition started version=%s", manifest.version)
        future = self._pool.submit(self._acquire, request, transport=self._transport_factory())
        future.add_done_callback(lambda task: self._deliver(lambda: self._acquired(task, result, root, startup)))

    def _acquired(self, task: Any, result: Any, root: Path, startup: bool) -> None:
        try:
            acquired = task.result()
        except Exception as exc:
            log.error("Update acquisition failed exception_type=%s", type(exc).__name__)
            self._install_failed(startup, "The update could not be prepared.")
            return
        if not acquired.succeeded:
            log.warning("Update acquisition refused code=%s", acquired.failure.code.value if acquired.failure else "unknown")
            self._install_failed(startup, "The update could not be verified.")
            return
        self._status("Installing update…")
        candidate = acquired.candidate
        future = self._pool.submit(self._install, candidate=candidate, installation_root=self._installation_root, data_root=self._data_root, transaction_root=root)
        future.add_done_callback(lambda task: self._deliver(lambda: self._installed(task, result, startup)))

    def _installed(self, task: Any, result: Any, startup: bool) -> None:
        try:
            installed = task.result()
        except Exception as exc:
            log.error("Update installation failed exception_type=%s", type(exc).__name__)
            self._install_failed(startup, "The update failed before completion.")
            return
        if installed.status is not SystemUpdateStatus.INSTALLED:
            log.warning("Update installation did not complete code=%s", installed.code.value if installed.code else "unknown")
            self._install_failed(startup, "HamChat restored the previous files or needs recovery.")
            return
        log.info("Update installation verified")
        self._close_splash(); self._activate_window()
        self._message(QMessageBox.Icon.Information, "HamChat updated", f"HamChat {result.manifest.version} is installed. HamChat will now restart.", (result.release_notes or "")[:_NOTES_LIMIT])
        if self._restart():
            log.info("Replacement process started")
            self._finish(True)
        else:
            log.warning("Replacement process could not be started")
            self._message(QMessageBox.Icon.Warning, "Restart HamChat", "The update is installed. Please close and reopen HamChat.")
            self._finish(True)

    def _install_failed(self, startup: bool, message: str) -> None:
        self._close_splash(); self._activate_window()
        self._message(QMessageBox.Icon.Warning, "HamChat update", message)
        self._finish_check(startup, False)

    def _finish_check(self, startup: bool, manual: bool, manual_error: str | None = None) -> None:
        if startup:
            self._close_splash(); self._clear_status(); self.startup_finished.emit(False)
        self._finish(False)

    def _finish(self, stop_old_process: bool) -> None:
        self._active = False; self.operation_changed.emit(False)
        if stop_old_process:
            self.startup_finished.emit(True)

    def _status(self, text: str) -> None:
        if self._splash is not None:
            self._splash.status(text)
        log.debug("Splash update phase=%s", text.split(" ", 1)[0])

    def _clear_status(self) -> None:
        if self._splash is not None:
            self._splash.clear_status()

    def _close_splash(self) -> None:
        if self._splash is not None:
            self._splash.close()

    def _activate_window(self) -> None:
        if self._window is not None:
            self._window.show(); self._window.raise_(); self._window.activateWindow()

    def _message(self, icon: QMessageBox.Icon, title: str, text: str, detail: str | None = None) -> None:
        box = QMessageBox(icon, title, text, parent=self._window)
        if detail:
            box.setDetailedText(detail[:_NOTES_LIMIT])
        box.exec()

    def _launch_replacement(self) -> bool:
        try:
            subprocess.Popen([sys.executable, Path(sys.argv[0]).resolve().as_posix(), *sys.argv[1:]], cwd=str(self._installation_root), start_new_session=True)
            return True
        except OSError:
            return False

    def shutdown(self) -> None:
        self._stopping = True
        self._pool.shutdown(wait=False, cancel_futures=True)
        log.debug("Update controller shutdown")
