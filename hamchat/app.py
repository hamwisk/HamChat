# hamchat/app.py
from __future__ import annotations
import logging, sys
from PyQt6.QtWidgets import QApplication
import argparse, logging, os, platform
from pathlib import Path
from enum import Enum
from multiprocessing import Process, Pipe
from .splash_worker import splash_process
from .db_init import ensure_database_ready
from .paths import default_data_dir, log_paths, settings_dir
from .settings import load_settings, set_admin_presence
from .logging_config import init_logging
from .constants import APP_NAME, __version__
from hamchat.ui.main_window import MainWindow
from hamchat.db_ops import read_db_mode, open_by_detection, probe_admin_exists


class RunMode(str, Enum):
    SOLO = "solo"      # 🥓 whole hog (local/solo)
    HAM = "ham"        # 🐖 server
    SNOUT = "snout"    # 🐽 agent

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

# --- stubs; wire your real implementations here ---
def run_solo(db_conn, db_mode_str):
    logging.getLogger("boot").info("Starting SOLO (🥓 whole hog) — launching MainWindow")
    app = QApplication(sys.argv)
    w = MainWindow(runtime_mode=RunMode.SOLO.value, db_conn=db_conn, db_mode=db_mode_str)
    w.show()
    app.exec()

def run_server():
    logging.getLogger("boot").info("Starting HAM server (🐖)")
    # TODO: import and start FastAPI (or your server) and block

def run_agent(server_url: str):
    logging.getLogger("boot").info("Starting SNOUT agent (🐽) — launching MainWindow bound to %s", server_url)
    app = QApplication(sys.argv)
    w = MainWindow(runtime_mode=RunMode.SNOUT.value, server_url=server_url)
    w.show()
    app.exec()

def main() -> int:
    args = parse_args()

    # Resolve mode early so we can skip heavy init for SNOUT
    mode = _resolve_mode(args)
    if mode is RunMode.SNOUT and not args.server_url:
        print("--snout requires --server-url", file=sys.stderr)
        return 2

    data_dir = Path(args.data_dir).expanduser().resolve() if args.data_dir else default_data_dir()
    logs_dir, log_path = log_paths(data_dir)
    settings_path = settings_dir().joinpath("app.json")
    cfg = load_settings(settings_path)

    level = (args.log_level or cfg["logging"]["level"]).upper()
    init_logging(
        logs_dir,
        level=level,
        max_bytes=int(cfg["logging"]["max_bytes"]),
        backup_count=int(cfg["logging"]["backup_count"]),
        also_console=(not args.no_console_log),
    )
    log = logging.getLogger("boot")
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

    try:
        # --- heavy init (skip for SNOUT/agent) ---
        if needs_local_init(mode):
            log.info("Initializing secure database...")
            if ensure_database_ready(data_dir, update_settings=True) != 0:
                parent_conn.send("close")
                splash_proc.join(timeout=3)
                log.error("Database initialization failed. Aborting.")
                return 1

            log.info("Loading configuration and models...")
            from hamchat.infra.llm.ollama_registry import refresh_registry
            log.info("Refreshing model registry (Ollama)…")
            registry = refresh_registry()
            log.info("Models available: %d", sum(1 for m in registry["models"] if m["available"]))

            # Re-load settings to pick up db_mode that init may have written
            conn, db_mode = open_by_detection(data_dir)
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
        parent_conn.send("close")
        splash_proc.join(timeout=3)
        raise

    # tell splash to close, wait briefly
    parent_conn.send("close")
    splash_proc.join(timeout=5)

    # continue into the chosen runtime
    if mode is RunMode.SNOUT:
        run_agent(args.server_url)
    elif mode is RunMode.HAM:
        run_server()
    else:
        run_solo(conn, db_mode)

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
