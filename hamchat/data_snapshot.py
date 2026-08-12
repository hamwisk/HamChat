"""Application-owned coherent open-SQLite/CAS snapshot provider.

Secure/strict SQLCipher snapshots are deliberately refused until its binding's
encrypted backup semantics are proven on supported deployments.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import threading
from typing import Iterator

from .db_ops import DB_FILENAME, CAS_MAGIC
from .update_assessment import PreservationClass
from .update_preservation import CapturedArtifact, _file_digest


class SnapshotStatus(str, Enum):
    COHERENT = "coherent"
    BLOCKED = "blocked"


class SnapshotFailureCode(str, Enum):
    INVALID_DATA_ROOT = "invalid_selected_data_root"
    INVALID_DESTINATION = "invalid_staging_destination"
    UNSUPPORTED_SECURITY_MODE = "encrypted_snapshot_safety_unsupported"
    DATABASE_UNAVAILABLE = "database_connection_unavailable"
    DATABASE_BACKUP_FAILED = "database_backup_failed"
    DATABASE_VERIFICATION_FAILED = "database_verification_failed"
    CAS_INVENTORY_UNAVAILABLE = "cas_inventory_unavailable"
    CAS_OBJECT_MISSING = "cas_object_missing"
    CAS_DIGEST_MISMATCH = "cas_logical_digest_mismatch"
    CAS_PATH_UNSAFE = "unsafe_cas_path"
    CAS_CAPTURE_FAILED = "cas_capture_failed"


@dataclass(frozen=True)
class SnapshotFailure:
    code: SnapshotFailureCode


@dataclass(frozen=True)
class DataSnapshotResult:
    status: SnapshotStatus
    artifacts: tuple[CapturedArtifact, ...] = ()
    failure: SnapshotFailure | None = None
    source_mutated: bool = False
    source_mutation_permitted: bool = False
    update_mutation_permitted: bool = False


_BARRIER = threading.RLock()


@contextmanager
def data_snapshot_barrier(timeout: float = 5.0) -> Iterator[None]:
    """In-process barrier; it cannot claim coordination with external writers."""
    if not _BARRIER.acquire(timeout=timeout):
        raise TimeoutError
    try:
        yield
    finally:
        _BARRIER.release()


class HamChatDataSnapshotProvider:
    """Captures the DB snapshot then exactly the CAS objects it references."""

    def capture(self, source_root: Path, destination: Path) -> tuple[CapturedArtifact, ...]:
        result = self.capture_result(source_root, destination)
        if result.status is not SnapshotStatus.COHERENT:
            raise RuntimeError(result.failure.code.value if result.failure else "snapshot_failed")
        return result.artifacts

    def capture_result(self, data_root: Path, destination: Path) -> DataSnapshotResult:
        data_root = Path(data_root)
        if not data_root.is_dir() or data_root.is_symlink() or destination.is_absolute() is False:
            return DataSnapshotResult(SnapshotStatus.BLOCKED, failure=SnapshotFailure(SnapshotFailureCode.INVALID_DATA_ROOT))
        db_path = data_root / DB_FILENAME
        if not db_path.is_file():
            return DataSnapshotResult(SnapshotStatus.BLOCKED, failure=SnapshotFailure(SnapshotFailureCode.DATABASE_UNAVAILABLE))
        try:
            with data_snapshot_barrier():
                source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                try:
                    mode_row = source.execute("SELECT value FROM meta WHERE key='db_mode'").fetchone()
                    mode = mode_row[0] if mode_row else "open"
                    if mode != "open":
                        return DataSnapshotResult(SnapshotStatus.BLOCKED, failure=SnapshotFailure(SnapshotFailureCode.UNSUPPORTED_SECURITY_MODE))
                    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
                    snapshot = destination / "ham_mem.db"
                    target = sqlite3.connect(snapshot)
                    try:
                        source.backup(target)
                        target.commit()
                    finally:
                        target.close()
                    os.chmod(snapshot, 0o600)
                    if sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True).execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        return DataSnapshotResult(SnapshotStatus.BLOCKED, failure=SnapshotFailure(SnapshotFailureCode.DATABASE_VERIFICATION_FAILED))
                    db_size, db_digest = _file_digest(snapshot)
                    artifacts = [CapturedArtifact("database", "data-snapshot/ham_mem.db", PreservationClass.EXTERNAL_AUTHORITATIVE_STATE.value, "sqlite_online_backup", db_size, db_digest, db_digest, True)]
                    inventory = [bytes(row[0]).hex() for row in source.execute("SELECT sha256 FROM files ORDER BY sha256")]
                    if len(inventory) != len(set(inventory)):
                        return DataSnapshotResult(SnapshotStatus.BLOCKED, failure=SnapshotFailure(SnapshotFailureCode.CAS_INVENTORY_UNAVAILABLE))
                    for logical in inventory:
                        if len(logical) != 64 or any(c not in "0123456789abcdef" for c in logical):
                            return DataSnapshotResult(SnapshotStatus.BLOCKED, failure=SnapshotFailure(SnapshotFailureCode.CAS_INVENTORY_UNAVAILABLE))
                        src = data_root / "cas" / logical
                        if not src.is_file() or src.is_symlink() or src.stat().st_nlink != 1:
                            return DataSnapshotResult(SnapshotStatus.BLOCKED, failure=SnapshotFailure(SnapshotFailureCode.CAS_OBJECT_MISSING))
                        raw = src.read_bytes()
                        if hashlib.sha256(raw).hexdigest() != logical:
                            return DataSnapshotResult(SnapshotStatus.BLOCKED, failure=SnapshotFailure(SnapshotFailureCode.CAS_DIGEST_MISMATCH))
                        out = destination / "cas" / logical; out.parent.mkdir(mode=0o700, exist_ok=True)
                        out.write_bytes(raw); os.chmod(out, 0o600)
                        size, digest = _file_digest(out)
                        artifacts.append(CapturedArtifact(f"cas:{logical}", f"data-snapshot/cas/{logical}", PreservationClass.EXTERNAL_AUTHORITATIVE_STATE.value, "exact_stored_byte_copy", size, digest, digest, True))
                    return DataSnapshotResult(SnapshotStatus.COHERENT, tuple(artifacts))
                finally:
                    source.close()
        except (sqlite3.Error, OSError, TimeoutError):
            return DataSnapshotResult(SnapshotStatus.BLOCKED, failure=SnapshotFailure(SnapshotFailureCode.DATABASE_BACKUP_FAILED))
