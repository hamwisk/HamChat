"""Verified, non-authorizing preservation bundles for a future updater.

This module never changes a source installation.  It only writes below the
explicit preservation root passed to :func:`create_verified_backup`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Callable, Iterable, Protocol

from .update_assessment import (
    InstallationAssessment,
    PreservationClass,
    PreservationPlan,
)
from .legacy_registry_migration import LegacyMigrationPlan, MigrationStatus


BACKUP_FORMAT_VERSION = 1
JOURNAL_FORMAT_VERSION = 1
_CHUNK_SIZE = 1024 * 1024
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}\Z")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")


class JournalState(str, Enum):
    PREPARING = "preparing"
    BACKUP_PUBLISHED = "backup_published"
    BACKUP_VERIFIED = "backup_verified"
    ABORTED = "aborted"
    DATA_PRESERVATION_NOT_REQUIRED = "data_preservation_not_required"
    SYSTEM_INSTALL_AUTHORIZED = "system_install_authorized"
    SYSTEM_INSTALL_STARTED = "system_install_started"
    SYSTEM_INSTALL_VERIFIED = "system_install_verified"
    COMPLETED = "completed"
    SYSTEM_ROLLBACK_REQUIRED = "system_rollback_required"
    SYSTEM_ROLLBACK_STARTED = "system_rollback_started"
    SYSTEM_ROLLBACK_VERIFIED = "system_rollback_verified"
    MANUAL_RECOVERY_REQUIRED = "manual_recovery_required"


class PreservationSourceKind(str, Enum):
    ORDINARY_FILE = "ordinary_file"
    DATA_SNAPSHOT = "data_snapshot"


class BackupFailureCode(str, Enum):
    INVALID_TRANSACTION_ID = "invalid_transaction_id"
    INVALID_INSTALLED_IDENTITY = "invalid_installed_identity"
    INVALID_TARGET_IDENTITY = "invalid_target_identity"
    ASSESSMENT_MISMATCH = "assessment_identity_mismatch"
    PLAN_MISMATCH = "preservation_plan_mismatch"
    INVALID_SOURCE_ROOT = "invalid_source_root"
    INVALID_BACKUP_ROOT = "invalid_backup_root"
    BACKUP_ROOT_OVERLAP = "backup_root_overlaps_source"
    PATH_ESCAPE = "path_escapes_permitted_root"
    UNEXPECTED_PATH = "unexpected_preservation_path"
    SOURCE_MISSING = "source_missing"
    SOURCE_TYPE_UNSUPPORTED = "source_type_unsupported"
    SOURCE_UNREADABLE = "source_unreadable"
    SOURCE_CHANGED = "source_changed_during_capture"
    SOURCE_TOO_LARGE = "source_too_large"
    TOTAL_TOO_LARGE = "total_capture_too_large"
    DATABASE_SNAPSHOT_UNAVAILABLE = "database_snapshot_unavailable"
    CAPTURE_FAILED = "capture_failed"
    DIGEST_MISMATCH = "digest_mismatch"
    MANIFEST_INVALID = "manifest_invalid"
    JOURNAL_INVALID = "journal_invalid"
    JOURNAL_TRANSITION_INVALID = "journal_transition_invalid"
    EXISTING_TRANSACTION_MISMATCH = "existing_transaction_mismatch"
    BUNDLE_CORRUPTED = "completed_bundle_corrupted"
    UNSUPPORTED_FORMAT = "unsupported_future_format"


@dataclass(frozen=True)
class BackupFailure:
    code: BackupFailureCode
    path: str | None = None


@dataclass(frozen=True)
class PreservationRequest:
    logical_id: str
    source_root: Path
    relative_path: Path
    backup_path: Path
    classification: PreservationClass
    kind: PreservationSourceKind = PreservationSourceKind.ORDINARY_FILE
    required: bool = True


@dataclass(frozen=True)
class CapturedArtifact:
    logical_id: str
    backup_path: str
    classification: str
    capture_method: str
    size: int
    digest: str
    source_digest: str
    required: bool


@dataclass(frozen=True)
class BackupTransaction:
    transaction_id: str
    source_root: Path
    data_root: Path
    preservation_root: Path
    installed_commit: str
    target_commit: str
    assessment_digest: str
    plan_digest: str
    requests: tuple[PreservationRequest, ...]
    assessment: InstallationAssessment | None = None
    preservation_plan: PreservationPlan | None = None
    legacy_migration_plans: tuple[LegacyMigrationPlan, ...] = ()
    max_file_bytes: int = 1024 * 1024 * 1024
    max_total_bytes: int = 10 * 1024 * 1024 * 1024


@dataclass(frozen=True)
class BackupResult:
    transaction_id: str
    journal_state: JournalState | None
    bundle_path: Path | None
    journal_path: Path
    verified: bool
    source_mutation_permitted: bool
    artifacts: tuple[CapturedArtifact, ...] = ()
    failure: BackupFailure | None = None


class SnapshotProvider(Protocol):
    """Provider for an application-consistent DB/CAS snapshot.

    The application must supply this while it owns database keys/connections;
    this module intentionally has no live-database copy fallback.
    """

    def capture(self, source_root: Path, destination: Path) -> tuple[CapturedArtifact, ...]: ...


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_relative(path: Path) -> bool:
    return not path.is_absolute() and ".." not in path.parts and path.parts != ()


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _transaction_paths(root: Path, transaction_id: str) -> tuple[Path, Path, Path]:
    return root / "bundles" / transaction_id, root / "journals" / f"{transaction_id}.json", root / ".staging" / transaction_id


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    encoded = _canonical(value)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _journal_data(transaction: BackupTransaction, state: JournalState, bundle: Path | None, manifest_digest: str | None, failure: BackupFailure | None = None) -> dict:
    return {
        "format_version": JOURNAL_FORMAT_VERSION,
        "transaction_id": transaction.transaction_id,
        "installed_commit": transaction.installed_commit,
        "target_commit": transaction.target_commit,
        "assessment_digest": transaction.assessment_digest,
        "plan_digest": transaction.plan_digest,
        "state": state.value,
        "bundle": bundle.name if bundle else None,
        "manifest_digest": manifest_digest,
        "failure": failure.code.value if failure else None,
    }


def _write_journal(transaction: BackupTransaction, state: JournalState, bundle: Path | None, manifest_digest: str | None, failure: BackupFailure | None = None) -> Path:
    _, path, _ = _transaction_paths(transaction.preservation_root, transaction.transaction_id)
    existing = load_journal(path)
    if existing is not None:
        if any(existing.get(key) != _journal_data(transaction, state, bundle, manifest_digest, failure).get(key) for key in ("transaction_id", "installed_commit", "target_commit", "assessment_digest", "plan_digest")):
            raise ValueError(BackupFailureCode.EXISTING_TRANSACTION_MISMATCH.value)
        current = JournalState(existing["state"])
        allowed = {JournalState.PREPARING: {JournalState.PREPARING, JournalState.BACKUP_PUBLISHED, JournalState.ABORTED}, JournalState.BACKUP_PUBLISHED: {JournalState.BACKUP_PUBLISHED, JournalState.BACKUP_VERIFIED, JournalState.ABORTED}, JournalState.BACKUP_VERIFIED: {JournalState.BACKUP_VERIFIED}, JournalState.ABORTED: {JournalState.ABORTED}}
        if state not in allowed[current]:
            raise ValueError(BackupFailureCode.JOURNAL_TRANSITION_INVALID.value)
        if current is state and existing != _journal_data(transaction, state, bundle, manifest_digest, failure):
            raise ValueError(BackupFailureCode.EXISTING_TRANSACTION_MISMATCH.value)
    _atomic_json(path, _journal_data(transaction, state, bundle, manifest_digest, failure))
    return path


def load_journal(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != {"format_version", "transaction_id", "installed_commit", "target_commit", "assessment_digest", "plan_digest", "state", "bundle", "manifest_digest", "failure"}:
            raise ValueError
        if value["format_version"] != JOURNAL_FORMAT_VERSION:
            raise ValueError
        if not _ID_RE.fullmatch(value["transaction_id"]) or not _COMMIT_RE.fullmatch(value["installed_commit"]) or not _COMMIT_RE.fullmatch(value["target_commit"]):
            raise ValueError
        JournalState(value["state"])
        return value
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ValueError(BackupFailureCode.JOURNAL_INVALID.value)


def _copy_file(request: PreservationRequest, destination: Path, total: int, transaction: BackupTransaction) -> tuple[CapturedArtifact, int]:
    if not _safe_relative(request.relative_path) or not _safe_relative(request.backup_path):
        raise ValueError(BackupFailureCode.PATH_ESCAPE.value)
    source = request.source_root / request.relative_path
    if not _within(source.parent, request.source_root) or source.is_symlink():
        raise ValueError(BackupFailureCode.PATH_ESCAPE.value)
    try:
        before = source.stat(follow_symlinks=False)
    except FileNotFoundError:
        if not request.required:
            raise FileNotFoundError
        raise ValueError(BackupFailureCode.SOURCE_MISSING.value)
    except OSError:
        raise ValueError(BackupFailureCode.SOURCE_UNREADABLE.value)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(BackupFailureCode.SOURCE_TYPE_UNSUPPORTED.value)
    if before.st_nlink != 1:
        raise ValueError(BackupFailureCode.SOURCE_TYPE_UNSUPPORTED.value)
    if before.st_size > transaction.max_file_bytes:
        raise ValueError(BackupFailureCode.SOURCE_TOO_LARGE.value)
    if total + before.st_size > transaction.max_total_bytes:
        raise ValueError(BackupFailureCode.TOTAL_TOO_LARGE.value)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(destination.parent, 0o700)
    source_hash = hashlib.sha256()
    captured_hash = hashlib.sha256()
    written = 0
    try:
        source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            after_open = os.fstat(source_fd)
            if (after_open.st_dev, after_open.st_ino, after_open.st_size, after_open.st_mtime_ns) != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
                raise ValueError(BackupFailureCode.SOURCE_CHANGED.value)
            destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                while chunk := os.read(source_fd, _CHUNK_SIZE):
                    source_hash.update(chunk); captured_hash.update(chunk)
                    os.write(destination_fd, chunk); written += len(chunk)
                os.fsync(destination_fd)
            finally:
                os.close(destination_fd)
        finally:
            os.close(source_fd)
    except ValueError:
        raise
    except OSError:
        raise ValueError(BackupFailureCode.CAPTURE_FAILED.value)
    after = source.stat(follow_symlinks=False)
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
        raise ValueError(BackupFailureCode.SOURCE_CHANGED.value)
    if written != before.st_size:
        raise ValueError(BackupFailureCode.SOURCE_CHANGED.value)
    digest = captured_hash.hexdigest()
    if source_hash.hexdigest() != digest:
        raise ValueError(BackupFailureCode.DIGEST_MISMATCH.value)
    reopened = _file_digest(destination)
    if reopened != (written, digest):
        raise ValueError(BackupFailureCode.DIGEST_MISMATCH.value)
    return CapturedArtifact(request.logical_id, request.backup_path.as_posix(), request.classification.value, "stream_copy", written, digest, digest, request.required), total + written


def _file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256(); size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK_SIZE):
            digest.update(chunk); size += len(chunk)
    return size, digest.hexdigest()


def _manifest(transaction: BackupTransaction, artifacts: Iterable[CapturedArtifact]) -> dict:
    # `verified` is integrity-bound.  It is intentionally not self-digested;
    # `manifest_digest` is the self-excluded field only.
    body = {"format_version": BACKUP_FORMAT_VERSION, "transaction_id": transaction.transaction_id, "installed_commit": transaction.installed_commit, "target_commit": transaction.target_commit, "assessment_digest": transaction.assessment_digest, "plan_digest": transaction.plan_digest, "legacy_plan_digests": [plan.projected_digest for plan in transaction.legacy_migration_plans if plan.status is MigrationStatus.READY], "artifacts": [artifact.__dict__ for artifact in artifacts], "verified": True}
    return {**body, "manifest_digest": _digest_bytes(_canonical(body))}


def _validate_transaction(transaction: BackupTransaction) -> BackupFailure | None:
    if not _ID_RE.fullmatch(transaction.transaction_id): return BackupFailure(BackupFailureCode.INVALID_TRANSACTION_ID)
    if not _COMMIT_RE.fullmatch(transaction.installed_commit): return BackupFailure(BackupFailureCode.INVALID_INSTALLED_IDENTITY)
    if not _COMMIT_RE.fullmatch(transaction.target_commit): return BackupFailure(BackupFailureCode.INVALID_TARGET_IDENTITY)
    if transaction.assessment is not None:
        assessment = transaction.assessment
        if (assessment.installation_root.resolve() != transaction.source_root.resolve() or assessment.data_dir.resolve() != transaction.data_root.resolve() or assessment.installed_commit != transaction.installed_commit or assessment.target_commit != transaction.target_commit):
            return BackupFailure(BackupFailureCode.ASSESSMENT_MISMATCH)
    if transaction.preservation_plan is not None and _digest_bytes(_canonical(_plan_identity(transaction.preservation_plan))) != transaction.plan_digest:
        return BackupFailure(BackupFailureCode.PLAN_MISMATCH)
    for migration in transaction.legacy_migration_plans:
        if migration.status is MigrationStatus.BLOCKED:
            return BackupFailure(BackupFailureCode.PLAN_MISMATCH)
        if migration.status is MigrationStatus.READY and (migration.installed_commit != transaction.installed_commit or migration.target_identity != transaction.target_commit or not migration.backup_required or migration.execution_permitted):
            return BackupFailure(BackupFailureCode.PLAN_MISMATCH)
    if not transaction.source_root.is_dir() or transaction.source_root.is_symlink(): return BackupFailure(BackupFailureCode.INVALID_SOURCE_ROOT)
    if transaction.preservation_root.exists() and not transaction.preservation_root.is_dir(): return BackupFailure(BackupFailureCode.INVALID_BACKUP_ROOT)
    if _within(transaction.preservation_root, transaction.source_root) or _within(transaction.preservation_root, transaction.data_root): return BackupFailure(BackupFailureCode.BACKUP_ROOT_OVERLAP)
    logical, paths = set(), set()
    for request in transaction.requests:
        if request.logical_id in logical or request.backup_path.as_posix() in paths: return BackupFailure(BackupFailureCode.UNEXPECTED_PATH)
        logical.add(request.logical_id); paths.add(request.backup_path.as_posix())
    return None


def _plan_identity(plan: PreservationPlan) -> dict:
    return {"blocked": plan.blocked, "future_phases": plan.future_phases, "findings": [(item.path.as_posix(), item.preservation.value, item.reason.value if item.reason else None) for item in plan.findings]}


def _migration_requests(transaction: BackupTransaction) -> tuple[PreservationRequest, ...]:
    requests = list(transaction.requests)
    for migration in transaction.legacy_migration_plans:
        if migration.status is not MigrationStatus.READY:
            continue
        for path in migration.preservation_paths:
            relative = Path(path)
            requests.append(PreservationRequest(
                f"legacy:{relative.as_posix()}", transaction.source_root, relative,
                Path("legacy") / relative, PreservationClass.LEGACY_TRACKED_CUSTOMIZATION,
                required=relative.name not in {"context_overrides.user.json", "modality_triggers.user.json"},
            ))
    return tuple(requests)


def verify_bundle(bundle: Path) -> tuple[tuple[CapturedArtifact, ...] | None, BackupFailure | None]:
    try:
        raw = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        required = {"format_version", "transaction_id", "installed_commit", "target_commit", "assessment_digest", "plan_digest", "legacy_plan_digests", "artifacts", "manifest_digest", "verified"}
        if not isinstance(raw, dict) or set(raw) != required or raw["format_version"] != BACKUP_FORMAT_VERSION or raw["verified"] is not True: raise ValueError
        body = {key: value for key, value in raw.items() if key != "manifest_digest"}
        if _digest_bytes(_canonical(body)) != raw["manifest_digest"]: return None, BackupFailure(BackupFailureCode.DIGEST_MISMATCH)
        artifacts = []
        seen = set()
        for item in raw["artifacts"]:
            if not isinstance(item, dict) or item["backup_path"] in seen or not _safe_relative(Path(item["backup_path"])): raise ValueError
            seen.add(item["backup_path"]); path = bundle / item["backup_path"]
            if not _within(path, bundle) or not path.is_file() or path.is_symlink() or _file_digest(path) != (item["size"], item["digest"]): return None, BackupFailure(BackupFailureCode.BUNDLE_CORRUPTED, item["backup_path"])
            artifacts.append(CapturedArtifact(**item))
        return tuple(artifacts), None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None, BackupFailure(BackupFailureCode.MANIFEST_INVALID)


def create_verified_backup(
    transaction: BackupTransaction,
    *,
    snapshot_provider: SnapshotProvider | None = None,
) -> BackupResult:
    """Explicitly create and independently verify a non-authorizing bundle."""
    bundle, journal, staging = _transaction_paths(transaction.preservation_root, transaction.transaction_id)
    failure = _validate_transaction(transaction)
    if failure: return BackupResult(transaction.transaction_id, None, None, journal, False, False, failure=failure)
    if bundle.exists():
        artifacts, existing_failure = verify_bundle(bundle)
        if existing_failure is None:
            try:
                existing = load_journal(journal)
                manifest_digest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))["manifest_digest"]
                if existing and existing["state"] == JournalState.BACKUP_VERIFIED.value and existing["manifest_digest"] == manifest_digest:
                    return BackupResult(transaction.transaction_id, JournalState.BACKUP_VERIFIED, bundle, journal, True, False, artifacts)
            except ValueError:
                pass
        return BackupResult(transaction.transaction_id, None, bundle, journal, False, False, failure=existing_failure or BackupFailure(BackupFailureCode.EXISTING_TRANSACTION_MISMATCH))
    if staging.exists(): return BackupResult(transaction.transaction_id, None, None, journal, False, False, failure=BackupFailure(BackupFailureCode.EXISTING_TRANSACTION_MISMATCH))
    try:
        transaction.preservation_root.mkdir(mode=0o700, parents=True, exist_ok=True); os.chmod(transaction.preservation_root, 0o700)
        _write_journal(transaction, JournalState.PREPARING, None, None)
        staging.mkdir(mode=0o700, parents=True)
        artifacts: list[CapturedArtifact] = []; total = 0
        for request in _migration_requests(transaction):
            if request.kind is PreservationSourceKind.DATA_SNAPSHOT:
                if snapshot_provider is None:
                    raise ValueError(BackupFailureCode.DATABASE_SNAPSHOT_UNAVAILABLE.value)
                try:
                    supplied = snapshot_provider.capture(
                        request.source_root, staging / request.backup_path,
                    )
                except Exception:
                    raise ValueError(BackupFailureCode.CAPTURE_FAILED.value)
                for artifact in supplied:
                    artifact_path = staging / artifact.backup_path
                    if not _within(artifact_path, staging) or not artifact_path.is_file():
                        raise ValueError(BackupFailureCode.CAPTURE_FAILED.value)
                    size, digest = _file_digest(artifact_path)
                    if size != artifact.size or digest != artifact.digest:
                        raise ValueError(BackupFailureCode.DIGEST_MISMATCH.value)
                    total += size
                    if total > transaction.max_total_bytes:
                        raise ValueError(BackupFailureCode.TOTAL_TOO_LARGE.value)
                    artifacts.append(artifact)
                continue
            try:
                artifact, total = _copy_file(
                    request, staging / request.backup_path, total, transaction,
                )
            except FileNotFoundError:
                # Optional absent user layers are a normal preservation input.
                continue
            artifacts.append(artifact)
        manifest = _manifest(transaction, artifacts)
        _atomic_json(staging / "manifest.json", manifest)
        if verify_bundle(staging)[1] is not None: raise ValueError(BackupFailureCode.MANIFEST_INVALID.value)
        bundle.parent.mkdir(mode=0o700, parents=True, exist_ok=True); os.replace(staging, bundle); _fsync_directory(bundle.parent)
        _write_journal(transaction, JournalState.BACKUP_PUBLISHED, bundle, manifest["manifest_digest"])
        verified, failure = verify_bundle(bundle)
        if failure: raise ValueError(failure.code.value)
        _write_journal(transaction, JournalState.BACKUP_VERIFIED, bundle, manifest["manifest_digest"])
        return BackupResult(transaction.transaction_id, JournalState.BACKUP_VERIFIED, bundle, journal, True, False, verified)
    except ValueError as error:
        try:
            code = BackupFailureCode(str(error))
        except ValueError:
            code = BackupFailureCode.CAPTURE_FAILED
        failure = BackupFailure(code)
        try: _write_journal(transaction, JournalState.ABORTED, bundle if bundle.exists() else None, None, failure)
        except ValueError: pass
        return BackupResult(transaction.transaction_id, JournalState.ABORTED, bundle if bundle.exists() else None, journal, False, False, failure=failure)


def requests_from_plan(assessment: InstallationAssessment, plan: PreservationPlan) -> tuple[PreservationRequest, ...]:
    """Map typed plan findings to explicit capture requests or provider-only data."""
    requests = []
    for finding in plan.findings:
        if finding.preservation in {PreservationClass.RELEASE_OWNED, PreservationClass.DERIVED_OR_DISPOSABLE, PreservationClass.AMBIGUOUS_OR_CONFLICTING, PreservationClass.USER_EXTENSION}:
            continue
        path = finding.path
        if path == assessment.data_dir:
            requests.append(PreservationRequest("authoritative-data", path, Path("."), Path("data-snapshot"), finding.preservation, PreservationSourceKind.DATA_SNAPSHOT))
            continue
        if not _within(path, assessment.installation_root):
            continue
        relative = path.resolve(strict=False).relative_to(assessment.installation_root.resolve(strict=False))
        requests.append(
            PreservationRequest(
                f"file:{relative.as_posix()}", assessment.installation_root,
                relative, Path("files") / relative, finding.preservation,
                required=path.name not in {
                    "context_overrides.user.json", "modality_triggers.user.json",
                },
            )
        )
    return tuple(sorted(requests, key=lambda item: item.logical_id))
