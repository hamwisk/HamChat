# hamchat/core/settings.py
from __future__ import annotations
import json
from pathlib import Path

class Settings:
    def __init__(self, path: Path):
        self.path = path
        self.data = {}
        self.load()

    def load(self):
        try:
            self.data = json.loads(self.path.read_text("utf-8"))
        except Exception:
            self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
