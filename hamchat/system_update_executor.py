"""Bounded system-file-only executor for verified data-neutral releases."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path, PurePosixPath
import shutil

from .updates import ReleaseManifest
from .update_preservation import _atomic_json, _digest_bytes, _canonical


class SystemUpdateStatus(str, Enum):
    INSTALLED = "installed"
    BLOCKED = "blocked"
    ROLLED_BACK = "rolled_back"


class SystemUpdateCode(str, Enum):
    DATA_COMPATIBILITY = "data_compatibility_blocked"
    UNSAFE_PAYLOAD = "unsafe_payload"
    STAGING_FAILED = "staging_failed"
    INSTALL_FAILED = "install_failed"
    STAGED_ARTIFACT_CHANGED = "staged_artifact_changed"
    TRANSACTION_NOT_READY = "transaction_not_ready"
    JOURNAL_INVALID = "journal_invalid"
    MANUAL_RECOVERY = "manual_recovery_required"


@dataclass(frozen=True)
class SystemUpdateResult:
    status: SystemUpdateStatus
    code: SystemUpdateCode | None = None
    source_mutation_permitted: bool = False
    user_data_mutation_permitted: bool = False


@dataclass(frozen=True)
class VerifiedStagedCandidate:
    transaction_id: str
    staging_root: Path
    manifest: ReleaseManifest
    artifacts: tuple[tuple[str, str], ...]  # normalized path, sha256
    data_preservation_not_required: bool


_READY = "data_preservation_not_required"
_AUTHORIZED = "system_install_authorized"
_STARTED = "system_install_started"
_VERIFIED = "system_install_verified"
_COMPLETED = "completed"
_ROLLBACK_REQUIRED = "system_rollback_required"
_ROLLBACK_STARTED = "system_rollback_started"
_ROLLBACK_VERIFIED = "system_rollback_verified"
_MANUAL = "manual_recovery_required"


def _journal_path(root: Path) -> Path:
    return root / "system-install.json"


def prepare_system_install(candidate: VerifiedStagedCandidate, transaction_root: Path) -> None:
    """Durably bind candidate inventory before any installation mutation."""
    if not candidate.data_preservation_not_required:
        raise ValueError("not data neutral")
    records = []
    for path, digest in candidate.artifacts:
        source = candidate.staging_root / path
        if not _safe_path(path) or not source.is_file() or source.is_symlink() or _digest_bytes(source.read_bytes()) != digest:
            raise ValueError("unsafe staged artifact")
        records.append({"ordinal": len(records), "path": path, "new": digest, "old": None, "checkpoint": "planned"})
    _atomic_json(_journal_path(transaction_root), {"transaction": candidate.transaction_id, "staging": str(candidate.staging_root.resolve()), "state": _AUTHORIZED, "files": records})


def _state(path):
    if not path.exists(): return None
    if not path.is_file() or path.is_symlink(): return "foreign"
    return _digest_bytes(path.read_bytes())


def _prepare_rollback(journal, installation_root, transaction_root):
    rollback = transaction_root / "rollback"
    rollback.mkdir(parents=True, exist_ok=True)
    for item in journal["files"]:
        target = installation_root / item["path"]
        old = _state(target)
        if old == "foreign": raise ValueError
        item["old"] = old
        if old:
            backup = rollback / item["path"]; backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists(): shutil.copyfile(target, backup)
            if _state(backup) != old or _state(target) != old: raise ValueError
            item["rollback"] = str(backup.relative_to(transaction_root))


def _load(root: Path, candidate: VerifiedStagedCandidate) -> dict | None:
    try:
        import json
        value = json.loads(_journal_path(root).read_text())
        if value["transaction"] != candidate.transaction_id or value["staging"] != str(candidate.staging_root.resolve()): return None
        if value["state"] not in {_AUTHORIZED, _STARTED, _VERIFIED, _COMPLETED, _ROLLBACK_REQUIRED, _ROLLBACK_STARTED, _ROLLBACK_VERIFIED, _MANUAL}: return None
        return value
    except (OSError, ValueError, KeyError, TypeError): return None


def _save(root: Path, journal: dict) -> None:
    _atomic_json(_journal_path(root), journal)


def _safe_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(path.parts) and not path.is_absolute() and ".." not in path.parts and path.parts[0] not in {"data", "settings"}


def install_system_files(*, manifest: ReleaseManifest, candidate_root: Path, installation_root: Path, data_root: Path, managed_paths: tuple[str, ...], transaction_root: Path) -> SystemUpdateResult:
    """Copy only declared managed regular files; rollback them on any failure."""
    if not manifest.data_compatibility.data_neutral:
        return SystemUpdateResult(SystemUpdateStatus.BLOCKED, SystemUpdateCode.DATA_COMPATIBILITY)
    candidate_root, installation_root, data_root, transaction_root = map(Path.resolve, (candidate_root, installation_root, data_root, transaction_root))
    if any(not _safe_path(path) for path in managed_paths):
        return SystemUpdateResult(SystemUpdateStatus.BLOCKED, SystemUpdateCode.UNSAFE_PAYLOAD)
    targets = [(candidate_root / path, installation_root / path) for path in managed_paths]
    if any(not src.is_file() or src.is_symlink() or dst == data_root or data_root in dst.parents for src, dst in targets):
        return SystemUpdateResult(SystemUpdateStatus.BLOCKED, SystemUpdateCode.UNSAFE_PAYLOAD)
    backup = transaction_root / "rollback"
    try:
        backup.mkdir(parents=True, exist_ok=False)
        for _, dst in targets:
            if dst.exists():
                item = backup / dst.relative_to(installation_root); item.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(dst, item)
        for src, dst in targets:
            dst.parent.mkdir(parents=True, exist_ok=True)
            temp = dst.with_name(dst.name + ".update-tmp")
            shutil.copyfile(src, temp); os.replace(temp, dst)
        return SystemUpdateResult(SystemUpdateStatus.INSTALLED)
    except OSError:
        for _, dst in targets:
            prior = backup / dst.relative_to(installation_root)
            if prior.is_file():
                shutil.copyfile(prior, dst)
        return SystemUpdateResult(SystemUpdateStatus.ROLLED_BACK, SystemUpdateCode.INSTALL_FAILED)


def install_verified_candidate(*, candidate: VerifiedStagedCandidate, installation_root: Path, data_root: Path, transaction_root: Path) -> SystemUpdateResult:
    """Install only the immutable staged bytes identified by a transaction."""
    journal = _load(transaction_root, candidate)
    if journal is None or journal["state"] != _AUTHORIZED:
        return SystemUpdateResult(SystemUpdateStatus.BLOCKED, SystemUpdateCode.TRANSACTION_NOT_READY)
    paths = []
    for path, digest in candidate.artifacts:
        source = candidate.staging_root / path
        if not _safe_path(path) or not source.is_file() or source.is_symlink():
            return SystemUpdateResult(SystemUpdateStatus.BLOCKED, SystemUpdateCode.UNSAFE_PAYLOAD)
        actual = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
        if actual != digest:
            return SystemUpdateResult(SystemUpdateStatus.BLOCKED, SystemUpdateCode.STAGED_ARTIFACT_CHANGED)
        paths.append(path)
    try:
        _prepare_rollback(journal, installation_root, transaction_root); _save(transaction_root, journal)
        journal["state"] = _STARTED; _save(transaction_root, journal)
        for item in journal["files"]:
            target = installation_root / item["path"]
            if _state(target) != item["old"]: raise ValueError
            item["checkpoint"] = "mutation_intent_persisted"; _save(transaction_root, journal)
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(target.name + ".update-tmp")
            shutil.copyfile(candidate.staging_root / item["path"], temp); os.replace(temp, target)
            if _state(target) != item["new"]: raise ValueError
            item["checkpoint"] = "target_verified"; _save(transaction_root, journal)
        journal["state"] = _VERIFIED; _save(transaction_root, journal); journal["state"] = _COMPLETED; _save(transaction_root, journal)
        return SystemUpdateResult(SystemUpdateStatus.INSTALLED)
    except (OSError, ValueError):
        journal["state"] = _ROLLBACK_REQUIRED; _save(transaction_root, journal)
        return recover_system_install(candidate=candidate, transaction_root=transaction_root, installation_root=installation_root)


def recover_system_install(*, candidate: VerifiedStagedCandidate, transaction_root: Path, installation_root: Path | None = None) -> SystemUpdateResult:
    journal = _load(transaction_root, candidate)
    if journal is None: return SystemUpdateResult(SystemUpdateStatus.BLOCKED, SystemUpdateCode.JOURNAL_INVALID)
    if journal["state"] == _COMPLETED: return SystemUpdateResult(SystemUpdateStatus.INSTALLED)
    if journal["state"] in {_ROLLBACK_VERIFIED, _MANUAL}: return SystemUpdateResult(SystemUpdateStatus.ROLLED_BACK if journal["state"] == _ROLLBACK_VERIFIED else SystemUpdateStatus.BLOCKED, SystemUpdateCode.MANUAL_RECOVERY if journal["state"] == _MANUAL else None)
    if installation_root is None: return SystemUpdateResult(SystemUpdateStatus.BLOCKED, SystemUpdateCode.MANUAL_RECOVERY)
    try:
        journal["state"] = _ROLLBACK_STARTED; _save(transaction_root, journal)
        for item in reversed(journal["files"]):
            target = Path(installation_root) / item["path"]; state = _state(target)
            if state == item["old"]: continue
            if state != item["new"]: raise ValueError
            if item["old"] is None:
                target.unlink()
            else:
                backup = transaction_root / item["rollback"]
                if _state(backup) != item["old"]: raise ValueError
                shutil.copyfile(backup, target)
            if _state(target) != item["old"]: raise ValueError
            item["checkpoint"] = "rollback_verified"; _save(transaction_root, journal)
        journal["state"] = _ROLLBACK_VERIFIED; _save(transaction_root, journal)
        return SystemUpdateResult(SystemUpdateStatus.ROLLED_BACK)
    except (OSError, ValueError, KeyError):
        journal["state"] = _MANUAL; _save(transaction_root, journal)
        return SystemUpdateResult(SystemUpdateStatus.BLOCKED, SystemUpdateCode.MANUAL_RECOVERY)
