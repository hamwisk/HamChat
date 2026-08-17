# hamchat/app.py
from __future__ import annotations
import logging, sys
import argparse, os, platform
import stat
from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox
from PyQt6.QtGui import QIcon
from pathlib import Path
from enum import Enum
from multiprocessing import Process, Pipe
from .splash_worker import splash_process
from .paths import default_data_dir, log_paths, settings_dir
from .logging_config import init_logging
from .constants import APP_NAME, __version__


_RETAINED_NULL_STREAMS: list[object] = []
_KNOWN_ROOT_LAUNCHERS = ("run_hamchat.sh", "setup_venv.sh")


def _ensure_flushable_standard_streams() -> None:
    """Replace only unusable inherited standard streams for a detached launch."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        broken = stream is None or getattr(stream, "closed", False)
        if not broken:
            flush = getattr(stream, "flush", None)
            if not callable(flush):
                broken = True
            else:
                try:
                    flush()
                except (OSError, ValueError):
                    broken = True
        if broken:
            replacement = open(os.devnull, "w", encoding="utf-8")
            _RETAINED_NULL_STREAMS.append(replacement)
            setattr(sys, name, replacement)


def repair_known_launcher_modes(installation_root: Path) -> tuple[Path, ...]:
    """Repair only launchers damaged by pre-2.7.4 byte-only replacement."""
    failures: list[Path] = []
    for name in _KNOWN_ROOT_LAUNCHERS:
        launcher = installation_root / name
        try:
            file_stat = os.stat(launcher, follow_symlinks=False)
            if not stat.S_ISREG(file_stat.st_mode):
                continue
            if stat.S_IMODE(file_stat.st_mode) != 0o755:
                os.chmod(launcher, 0o755, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError:
            failures.append(launcher)
    return tuple(failures)


class RunMode(str, Enum):
    SOLO = "solo"      # 🥓 whole hog (local/solo)
    HAM = "ham"        # 🐖 server
    SNOUT = "snout"    # 🐽 agent

def get_app_icon() -> QIcon:
    # hamchat/app.py → parent is hamchat/, then /ui/ham_ico.ico
    icon_path = Path(__file__).resolve().parent / "ui" / "ham_ico.ico"
    if icon_path.exists():
        return QIcon(str(icon_path))
    return QIcon()

def _resolve_mode(args: argparse.Namespace) -> RunMode:
    # Primary ham-themed flags
    ham = bool(args.ham)
    snout = bool(args.snout)

    # Back-compat (hidden) flags; warn and map to new flags
    if getattr(args, "server", False):
        logging.warning("Deprecated: --server → use --ham")
        ham = True
    if getattr(args, "agent", False):
        logging.warning("Deprecated: --agent → use --snout")
        snout = True

    if ham and snout:
        raise SystemExit("Choose one mode: either --ham (server) or --snout (agent), not both.")
    if ham:
        return RunMode.HAM
    if snout:
        return RunMode.SNOUT

    # Env override for ops/containers (optional)
    env_mode = os.getenv("HAMCHAT_MODE", "").lower()
    if env_mode in (m.value for m in RunMode):
        return RunMode(env_mode)

    return RunMode.SOLO  # default

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog=APP_NAME, description="HamChat loader")
    # ham-themed public flags
    p.add_argument("--ham", action="store_true", help="Run in server mode 🐖")
    p.add_argument("--snout", action="store_true", help="Run in agent mode 🐽 (requires --server-url)")
    p.add_argument("--server-url", type=str, help="Server URL for agent mode, e.g. http://localhost:8080")

    # logging / paths
    p.add_argument("--data-dir", type=str, default=None, help="Override data directory")
    p.add_argument("--log-level", type=str, default=None, help="DEBUG, INFO, WARNING, ERROR")
    p.add_argument("--no-console-log", action="store_true", help="Disable console logging")

    # hidden back-compat flags (1.0)
    p.add_argument("--server", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--agent", action="store_true", help=argparse.SUPPRESS)

    return p.parse_args()

def needs_local_init(mode: RunMode) -> bool:
    return mode in (RunMode.SOLO, RunMode.HAM)

# --- Runtime implementations here ---
def run_solo(app: QApplication, db_conn, db_mode_str, data_dir: Path, update_controller=None):
    logging.getLogger("boot").info("Starting SOLO (🥓 whole hog) — launching MainWindow")
    app.setWindowIcon(get_app_icon())
    from hamchat.ui.main_window import MainWindow
    w = MainWindow(
        runtime_mode=RunMode.SOLO.value, db_conn=db_conn, db_mode=db_mode_str,
        data_dir=data_dir, update_controller=update_controller,
    )
    if update_controller is not None:
        update_controller.attach_window(w)
    w.show()
    app.exec()

def run_server():
    logging.getLogger("boot").info("Starting HAM server (🐖)")
    # TODO: import and start FastAPI (or your server) and block

def run_agent(app: QApplication, server_url: str):
    logging.getLogger("boot").info("Starting SNOUT agent (🐽) — launching MainWindow bound to %s", server_url)
    from hamchat.ui.main_window import MainWindow
    w = MainWindow(runtime_mode=RunMode.SNOUT.value, server_url=server_url)
    w.show()
    app.exec()


class _SplashBridge:
    """Bounded parent-side protocol for the separate startup splash."""
    def __init__(self, conn, process, log: logging.Logger):
        self._conn, self._process, self._log, self._closed = conn, process, log, False

    def status(self, text: str) -> None:
        if not self._closed and isinstance(text, str) and len(text) <= 120:
            self._conn.send({"type": "status", "text": text})

    def clear_status(self) -> None:
        if not self._closed:
            self._conn.send({"type": "clear_status"})

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._conn.send({"type": "close"})
            self._process.join(timeout=5)
            if self._process.is_alive():
                self._log.warning("Splash closure timed out")
        except (BrokenPipeError, EOFError, OSError):
            self._log.warning("Splash close request failed")

def main() -> int:
    # The previous updater may have launched us with a detached but invalid
    # terminal.  Do this before multiprocessing and logging setup.
    _ensure_flushable_standard_streams()
    launcher_repair_failures = repair_known_launcher_modes(Path.cwd())
    args = parse_args()

    # Resolve mode early so we can skip heavy init for SNOUT
    mode = _resolve_mode(args)
    if mode is RunMode.SNOUT and not args.server_url:
        print("--snout requires --server-url", file=sys.stderr)
        return 2

    data_dir = Path(args.data_dir).expanduser().resolve() if args.data_dir else default_data_dir()
    logs_dir, log_path = log_paths(data_dir)
    settings_path = Path.cwd().resolve() / "settings" / "app.json"
    # Logging is allowed in the minimal bootstrap; ordinary settings are not
    # loaded until recovery has made startup safe.
    level = (args.log_level or "INFO").upper()
    init_logging(
        logs_dir,
        level=level,
        max_bytes=10 * 1024 * 1024,
        backup_count=5,
        also_console=(not args.no_console_log),
    )
    log = logging.getLogger("boot")
    for launcher in launcher_repair_failures:
        log.warning("Could not repair launcher permissions for %s", launcher)
    log.info("=== %s %s starting ===", APP_NAME, __version__)
    log.info("Platform: %s | Python: %s", platform.platform(), platform.python_version())
    log.info("Data dir: %s | Log file: %s", data_dir, log_path)
    log.info("Settings: %s", settings_path)
    log.info("Resolved mode: %s", mode.value)

    # --- instant splash (we keep it for all modes; it’s cheap) ---
    parent_conn, child_conn = Pipe()
    splash_proc = Process(target=splash_process, args=(child_conn, "hamchat/ui/logo.png"))
    splash_proc.daemon = True
    splash_proc.start()
    log.info("Splash process started (pid %s)", splash_proc.pid)
    if not parent_conn.poll(3) or parent_conn.recv() != {"type": "ready"}:
        log.warning("Splash readiness was not confirmed")
    else:
        log.info("updates splash visibly ready")
    splash = _SplashBridge(parent_conn, splash_proc, log)

    # Recovery is intentionally before database/config/model imports.  It uses
    # only the durable system-file journal and rollback artifacts.
    from hamchat.system_update_executor import SystemUpdateStatus, recover_pending_system_installs
    from hamchat.update_controller import UpdateController, transaction_parent_for_installation
    splash.status("Checking previous update…")
    log.info("updates recovery check started")
    recovery = recover_pending_system_installs(
        transaction_parent=transaction_parent_for_installation(Path.cwd()),
        installation_root=Path.cwd(),
    )
    if recovery is not None:
        log.info("updates recovery outcome status=%s code=%s", recovery.status.value, recovery.code.value if recovery.code else "none")
    if recovery is not None and recovery.status is SystemUpdateStatus.BLOCKED:
        splash.close()
        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(None, "HamChat update recovery", "HamChat needs manual update recovery before it can start.")
        return 1

    # SOLO's policy is resolved while the splash is still visible, before any
    # user-data or model initialization.  The controller worker keeps fetches
    # and acquisition off the Qt GUI thread.
    app = QApplication.instance() or QApplication(sys.argv)
    controller = None
    if mode is RunMode.SOLO:
        controller = UpdateController(settings_path=settings_path, installation_root=Path.cwd(), data_root=data_dir, splash=splash)
        loop = QEventLoop()
        outcome = {"stop": False}
        def _startup_done(stop: bool) -> None:
            outcome["stop"] = stop; loop.quit()
        controller.startup_finished.connect(_startup_done)
        QTimer.singleShot(0, controller.start_startup_check)
        loop.exec()
        if outcome["stop"]:
            controller.shutdown()
            return 0

    try:
        # --- heavy init (skip for SNOUT/agent) ---
        if needs_local_init(mode):
            from .settings import load_settings, set_admin_presence
            from .db_ops import open_by_detection, probe_admin_exists

            log.info("Initializing database...")
            try:
                conn, db_mode = open_by_detection(data_dir)
            except Exception:
                splash.close()
                log.exception("Database initialization failed. Aborting.")
                return 1

            log.info("Loading configuration and models...")
            from hamchat.infra.llm.ollama_registry import refresh_registry
            log.info("Refreshing model registry (Ollama)…")
            registry = refresh_registry()
            log.info("Models available: %d", sum(1 for m in registry["models"] if m["available"]))

            has_admin: bool | None = None
            try:
                has_admin = probe_admin_exists(conn)  # returns True/False
            except Exception:
                has_admin = None   # unknown on error
            cfg = load_settings(settings_path)
            set_admin_presence(settings_path, cfg, has_admin)
        else:
            log.info("Agent mode detected; skipping local DB/model checks.")

        log.info("Initialization complete.")
    except Exception:
        log.exception("Fatal error during startup")
        splash.close()
        raise

    # tell splash to close, wait briefly
    splash.clear_status()
    splash.close()

    # continue into the chosen runtime
    if mode is RunMode.SNOUT:
        run_agent(app, args.server_url)
    elif mode is RunMode.HAM:
        run_server()
    else:
        run_solo(app, conn, db_mode, data_dir, controller)

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
