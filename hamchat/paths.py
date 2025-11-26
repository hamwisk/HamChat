# hamchat/paths.py
from __future__ import annotations
import os
from pathlib import Path
from typing import Tuple
from .constants import APP_NAME

def default_data_dir() -> Path:
    # Keep it simple & portable: local "data" folder by default.
    # Can be overridden via env HAMCHAT_DATA_DIR.
    env = os.getenv("HAMCHAT_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return Path("data").resolve()

def log_paths(data_dir: Path) -> Tuple[Path, Path]:
    logs_dir = data_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir, logs_dir / "app.log"

def settings_dir(project_root: Path | None = None) -> Path:
    # Non-sensitive JSON settings live here.
    base = Path(project_root) if project_root else Path(".")
    s = base.resolve() / "settings"
    s.mkdir(parents=True, exist_ok=True)
    return s
