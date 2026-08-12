from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3

import pytest

from hamchat.data_snapshot import HamChatDataSnapshotProvider, SnapshotStatus


def make_data(root: Path, *, mode="open", payload=b"attachment"):
    root.mkdir(); conn = sqlite3.connect(root / "ham_mem.db")
    conn.execute("CREATE TABLE meta (key TEXT, value TEXT)")
    conn.execute("CREATE TABLE files (sha256 BLOB)")
    conn.execute("INSERT INTO meta VALUES ('db_mode', ?)", (mode,))
    digest = hashlib.sha256(payload).hexdigest()
    conn.execute("INSERT INTO files VALUES (?)", (bytes.fromhex(digest),)); conn.commit(); conn.close()
    (root / "cas").mkdir(); (root / "cas" / digest).write_bytes(payload)
    return digest


def test_open_database_and_referenced_cas_snapshot_are_coherent(tmp_path):
    root = tmp_path / "data"; digest = make_data(root)
    result = HamChatDataSnapshotProvider().capture_result(root, tmp_path / "stage")
    assert result.status is SnapshotStatus.COHERENT
    assert {item.logical_id for item in result.artifacts} == {"database", f"cas:{digest}"}
    assert not result.source_mutated and not result.update_mutation_permitted
    assert (tmp_path / "stage/cas" / digest).read_bytes() == b"attachment"


@pytest.mark.parametrize("mode, missing", [("secure", False), ("strict", False), ("open", True)])
def test_unsupported_encryption_and_missing_cas_block(tmp_path, mode, missing):
    root = tmp_path / "data"; digest = make_data(root, mode=mode)
    if missing: (root / "cas" / digest).unlink()
    result = HamChatDataSnapshotProvider().capture_result(root, tmp_path / "stage")
    assert result.status is SnapshotStatus.BLOCKED


def test_unreferenced_cas_and_temp_files_are_excluded(tmp_path):
    root = tmp_path / "data"; make_data(root)
    (root / "cas" / ("f" * 64)).write_bytes(b"unreferenced")
    (root / "cas_tmp").mkdir(); (root / "cas_tmp" / "x").write_bytes(b"cache")
    result = HamChatDataSnapshotProvider().capture_result(root, tmp_path / "stage")
    assert result.status is SnapshotStatus.COHERENT
    assert len(result.artifacts) == 2
