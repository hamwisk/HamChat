from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from hamchat import db_ops
from hamchat.paths import default_data_dir
from hamchat.ui.theme import DEFAULT_THEME, load_shipped_theme, load_theme


class Cursor:
    def __init__(self, sha):
        self.sha = sha
        self.row = None

    def execute(self, query, args=()):
        if query.startswith("SELECT id, original_name"):
            self.row = None
        elif query.startswith("SELECT id FROM files"):
            self.row = (1,)
        elif query.startswith("SELECT sha256"):
            self.row = (bytes.fromhex(self.sha),)
        return self

    def fetchone(self):
        return self.row


class Database:
    def __init__(self, sha):
        self.sha = sha

    def cursor(self):
        return Cursor(self.sha)


def test_default_data_dir_environment_and_relative_path_policy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HAMCHAT_DATA_DIR", raising=False)
    assert default_data_dir() == (tmp_path / "data").resolve()
    monkeypatch.setenv("HAMCHAT_DATA_DIR", "external-data")
    assert default_data_dir() == (tmp_path / "external-data").resolve()


def test_open_by_detection_passes_the_selected_root(monkeypatch, tmp_path):
    selected = tmp_path / "external"
    captured = []
    monkeypatch.setattr(db_ops, "init_and_open", lambda data_dir: (captured.append(Path(data_dir)) or (object(), "open")))
    assert db_ops.open_by_detection(selected)[1] == "open"
    assert captured == [selected]


def test_explicit_database_root_failure_does_not_fall_back_to_cwd(monkeypatch, tmp_path):
    selected = tmp_path / "unavailable"
    monkeypatch.setattr(db_ops, "ensure_database_ready", lambda data_dir: 1)
    with pytest.raises(RuntimeError):
        db_ops.init_and_open(selected)
    assert not (tmp_path / "data").exists()


def test_cas_operations_use_only_explicit_data_root(tmp_path, monkeypatch):
    selected, other = tmp_path / "selected", tmp_path / "other"
    source = tmp_path / "source.bin"; source.write_bytes(b"payload")
    sha = "a" * 64
    monkeypatch.setattr(db_ops, "read_db_mode", lambda _db: "open")
    db = Database(sha)
    db_ops.cas_put(db, data_dir=selected, sha256=sha, mime="application/octet-stream", src_path=str(source))
    assert (selected / "cas" / sha).read_bytes() == b"payload"
    assert not (other / "cas").exists()
    assert db_ops.cas_path_for_file(db, 1, data_dir=selected) == selected / "cas" / sha


def test_shipped_theme_loader_never_writes_valid_missing_or_malformed_files(tmp_path, caplog):
    themes = tmp_path / "themes"; themes.mkdir()
    shipped = themes / "default_theme.json"
    content = json.dumps({"name": "custom", "variants": {}}).encode()
    shipped.write_bytes(content)
    before = shipped.stat().st_mtime_ns
    assert load_shipped_theme(themes)["name"] == "custom"
    assert shipped.read_bytes() == content and shipped.stat().st_mtime_ns == before
    shipped.unlink()
    assert load_shipped_theme(themes) == DEFAULT_THEME
    assert not shipped.exists()
    shipped.write_text("not json", encoding="utf-8")
    malformed = shipped.read_bytes()
    assert load_shipped_theme(themes) == DEFAULT_THEME
    assert shipped.read_bytes() == malformed
    assert "in-memory fallback" in caplog.text


def test_custom_theme_failure_is_preserved_and_uses_memory_fallback(tmp_path):
    custom = tmp_path / "mine.json"; custom.write_text("not json", encoding="utf-8")
    original = custom.read_bytes()
    assert load_theme(custom) == DEFAULT_THEME
    assert custom.read_bytes() == original
    valid = tmp_path / "valid.json"; valid.write_text(json.dumps({"name": "mine", "variants": {}}), encoding="utf-8")
    assert load_theme(valid)["name"] == "mine"
