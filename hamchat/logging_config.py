# hamchat/logging_config.py
from __future__ import annotations
import logging, logging.handlers, sys, os, traceback
from pathlib import Path
from datetime import datetime
from typing import Optional
from .constants import DEFAULT_LOG_MAX_BYTES, DEFAULT_LOG_BACKUP_COUNT, DEFAULT_LOG_FILENAME

class _ConsoleFormatter(logging.Formatter):
    # Minimal colorization without external deps
    COLORS = {
        "DEBUG": "\x1b[36m",
        "INFO": "\x1b[32m",
        "WARNING": "\x1b[33m",
        "ERROR": "\x1b[31m",
        "CRITICAL": "\x1b[41m",
    }
    RESET = "\x1b[0m"
    def format(self, record: logging.LogRecord) -> str:
        level = record.levelname
        color = self.COLORS.get(level, "")
        reset = self.RESET if color else ""
        base = f"{datetime.fromtimestamp(record.created).isoformat(timespec='seconds')} | {record.levelname:<8} | {record.name} | {record.getMessage()}"
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        if sys.stdout.isatty():
            return f"{color}{base}{reset}"
        return base

class _FileFormatter(logging.Formatter):
    def __init__(self):
        super().__init__("%(asctime)s | %(levelname)s | %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S")

def init_logging(log_dir: Path, level: str = "INFO", log_name: str = DEFAULT_LOG_FILENAME,
                 max_bytes: int = DEFAULT_LOG_MAX_BYTES, backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
                 also_console: bool = True) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / log_name

    root = logging.getLogger()
    # Clear old handlers to support re-init in tests
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # File handler (rotating)
    fh = logging.handlers.RotatingFileHandler(str(log_path), maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8", delay=True)
    fh.setFormatter(_FileFormatter())
    fh.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(fh)

    # Console (optional)
    if also_console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(_ConsoleFormatter())
        ch.setLevel(getattr(logging, level.upper(), logging.INFO))
        root.addHandler(ch)

    # Reduce noise from noisy libs
    for noisy in ("asyncio", "urllib3", "httpx", "PIL", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Hook uncaught exceptions
    install_excepthook()

    logging.getLogger(__name__).info("Logging initialized → %s", log_path)
    return log_path

def install_excepthook():
    def _hook(exc_type, exc, tb):
        logger = logging.getLogger("uncaught")
        logger.error("Uncaught exception", exc_info=(exc_type, exc, tb))
        # Also print a compact message to stderr for the console
        try:
            import traceback as _tb
            msg = "".join(_tb.format_exception_only(exc_type, exc)).strip()
            sys.stderr.write(f"\nFATAL: {msg}\n")
            sys.stderr.flush()
        except Exception:
            pass
    sys.excepthook = _hook
