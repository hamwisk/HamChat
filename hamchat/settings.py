# hamchat/settings.py
from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict
from .constants import DEFAULT_LOG_MAX_BYTES, DEFAULT_LOG_BACKUP_COUNT

DEFAULT_SETTINGS: Dict[str, Any] = {
    "schema": 1,
    "logging": {
        "level": "INFO",
        "max_bytes": DEFAULT_LOG_MAX_BYTES,
        "backup_count": DEFAULT_LOG_BACKUP_COUNT
    },
    "ui": {"theme": "default"},
    "auth": {
        "has_admin": None,
        "signup_submit": False
    }
}

def load_settings(path: Path) -> dict:
    if not path.exists():
        save_settings(path, DEFAULT_SETTINGS)
        return DEFAULT_SETTINGS.copy()
    with path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    # Simple forward-fill of missing keys
    def merge(a: dict, b: dict):
        for k, v in b.items():
            if k not in a:
                a[k] = v
            elif isinstance(v, dict) and isinstance(a.get(k), dict):
                merge(a[k], v)
    merged = dict(cfg)
    merge(merged, DEFAULT_SETTINGS)
    return merged

def save_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    tmp.replace(path)

def set_security_mode(path: Path, cfg: dict, mode: str) -> dict:
    updated = dict(cfg)
    sec = dict(updated.get("security", {}))
    if sec.get("mode") != mode:
        sec["mode"] = mode
        updated["security"] = sec
        save_settings(path, updated)
    return updated

def set_admin_presence(path: Path, cfg: dict, has_admin: bool | None) -> dict:
    updated = dict(cfg)
    auth = dict(updated.get("auth", {}))
    if auth.get("has_admin") != has_admin:
        auth["has_admin"] = has_admin
        updated["auth"] = auth
        save_settings(path, updated)
    return updated