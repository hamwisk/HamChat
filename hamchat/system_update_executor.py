"""Bounded system-file-only executor for verified data-neutral releases."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import json

from .updates import ReleaseManifest, release_manifest_digest
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
_TERMINAL_STATES = {_AUTHORIZED, _COMPLETED, _ROLLBACK_VERIFIED}
_RECOVERABLE_STATES = {_STARTED, _VERIFIED, _ROLLBACK_REQUIRED, _ROLLBACK_STARTED}


def _journal_path(root: Path) -> Path:
    return root / "system-install.json"


def _is_regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.stat(path, follow_symlinks=False).st_mode)
    except FileNotFoundError:
        return False


def _is_safe_transaction_root(root: Path, parent: Path) -> bool:
    """Require a direct, non-symlink child of the transaction parent."""
    try:
        parent_stat = os.stat(parent, follow_symlinks=False)
        root_stat = os.stat(root, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(parent_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        return False
    try:
        return root.parent == parent and root.resolve().parent == parent.resolve()
    except OSError:
        return False


def _valid_journal(value: object) -> bool:
    """Validate recovery data before it may be classified or discarded."""
    if not isinstance(value, dict):
        return False
    def digest(item: object) -> bool:
        return isinstance(item, str) and len(item) == 64 and all(char in "0123456789abcdef" for char in item)

    if (not isinstance(value.get("transaction"), str) or not value["transaction"]
            or not isinstance(value.get("staging"), str)
            or not digest(value.get("manifest_digest"))
            or value.get("state") not in _TERMINAL_STATES | _RECOVERABLE_STATES | {_MANUAL}
            or not isinstance(value.get("files"), list)):
        return False
    for item in value["files"]:
        if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                or not _safe_path(item["path"])
                or not digest(item.get("new"))
                or item.get("old") is not None and not digest(item["old"])):
            return False
        rollback = item.get("rollback")
        if rollback is not None and (not isinstance(rollback, str) or not _safe_path(rollback)):
            return False
        mode = item.get("old_mode")
        if mode is not None and (type(mode) is not int or not 0 <= mode <= 0o777):
            return False
    return True


def _retire_transaction(root: Path, parent: Path) -> bool:
    """Remove only a validated direct transaction directory, never a link."""
    if not _is_safe_transaction_root(root, parent):
        return False
    try:
        shutil.rmtree(root)
    except OSError:
        return False
    return not os.path.lexists(root)


def _retire_if_safe(root: Path) -> None:
    """Best-effort retirement after a terminal state is durably recorded."""
    _retire_transaction(root, root.parent)


def prepare_system_install(candidate: VerifiedStagedCandidate, transaction_root: Path) -> None:
    """Durably bind candidate inventory before any installation mutation."""
    # This is derived from trusted candidate metadata, never from the
    # descriptive boolean retained on VerifiedStagedCandidate for results.
    if not candidate.manifest.data_compatibility.data_neutral:
        raise ValueError("not data neutral")
    records = []
    for path, digest in candidate.artifacts:
        source = candidate.staging_root / path
        if not _safe_path(path) or not source.is_file() or source.is_symlink() or _digest_bytes(source.read_bytes()) != digest:
            raise ValueError("unsafe staged artifact")
        records.append({"ordinal": len(records), "path": path, "new": digest, "old": None,
                        "old_mode": None, "checkpoint": "planned"})
    _atomic_json(_journal_path(transaction_root), {"transaction": candidate.transaction_id, "staging": str(candidate.staging_root.resolve()), "manifest_digest": release_manifest_digest(candidate.manifest), "state": _AUTHORIZED, "files": records})


def _state(path):
    try:
        file_stat = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(file_stat.st_mode):
        return "foreign"
    return _digest_bytes(path.read_bytes())


def _mode(path: Path) -> int | None:
    """Return a regular file's Unix permissions without following links."""
    try:
        file_stat = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(file_stat.st_mode):
        raise ValueError("managed target is not a regular file")
    return stat.S_IMODE(file_stat.st_mode)


def _replace_file(source: Path, target: Path, mode: int) -> None:
    """Atomically replace a regular file, retaining its intended mode."""
    temporary = target.with_name(target.name + ".update-tmp")
    # A stale or substituted temporary file must never be followed.
    try:
        os.stat(temporary, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise ValueError("update temporary already exists")
    try:
        shutil.copyfile(source, temporary)
        os.chmod(temporary, mode, follow_symlinks=False)
        os.replace(temporary, target)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _rollback_mode(item: dict, backup: Path) -> int:
    """Read new journal mode data, or use the backup mode from old journals."""
    mode = item.get("old_mode")
    if mode is None:
        mode = _mode(backup)
    if not isinstance(mode, int):
        raise ValueError("invalid rollback mode")
    return mode


def _prepare_rollback(journal, installation_root, transaction_root):
    rollback = transaction_root / "rollback"
    rollback.mkdir(parents=True, exist_ok=True)
    for item in journal["files"]:
        target = installation_root / item["path"]
        old = _state(target)
        if old == "foreign": raise ValueError
        item["old"] = old
        item["old_mode"] = _mode(target) if old else None
        if old:
            backup = rollback / item["path"]; backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                shutil.copyfile(target, backup)
                os.chmod(backup, item["old_mode"], follow_symlinks=False)
            if (_state(backup) != old or _state(target) != old
                    or _mode(backup) != item["old_mode"]):
                raise ValueError
            item["rollback"] = str(backup.relative_to(transaction_root))


def _load(root: Path, candidate: VerifiedStagedCandidate) -> dict | None:
    try:
        value = json.loads(_journal_path(root).read_text())
        if (value["transaction"] != candidate.transaction_id or value["staging"] != str(candidate.staging_root.resolve())
                or value["manifest_digest"] != release_manifest_digest(candidate.manifest)): return None
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
            if _state(dst) == "foreign":
                raise ValueError
            if _state(dst) is not None:
                item = backup / dst.relative_to(installation_root); item.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(dst, item)
        for src, dst in targets:
            dst.parent.mkdir(parents=True, exist_ok=True)
            expected_mode = _mode(dst) if _state(dst) is not None else _mode(src)
            expected_state = _state(src)
            _replace_file(src, dst, expected_mode)
            if _state(dst) != expected_state or _mode(dst) != expected_mode:
                raise ValueError
        return SystemUpdateResult(SystemUpdateStatus.INSTALLED)
    except (OSError, ValueError):
        for _, dst in targets:
            prior = backup / dst.relative_to(installation_root)
            if prior.is_file():
                prior_mode = _mode(prior)
                _replace_file(prior, dst, prior_mode)
                if _state(dst) != _state(prior) or _mode(dst) != prior_mode:
                    raise ValueError
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
            intended_mode = item.get("old_mode") if item["old"] is not None else _mode(candidate.staging_root / item["path"])
            if not isinstance(intended_mode, int): raise ValueError
            _replace_file(candidate.staging_root / item["path"], target, intended_mode)
            if _state(target) != item["new"] or _mode(target) != intended_mode: raise ValueError
            item["checkpoint"] = "target_verified"; _save(transaction_root, journal)
        journal["state"] = _VERIFIED; _save(transaction_root, journal); journal["state"] = _COMPLETED; _save(transaction_root, journal)
        _retire_if_safe(transaction_root)
        return SystemUpdateResult(SystemUpdateStatus.INSTALLED)
    except (OSError, ValueError):
        journal["state"] = _ROLLBACK_REQUIRED; _save(transaction_root, journal)
        return recover_system_install(candidate=candidate, transaction_root=transaction_root, installation_root=installation_root)


def recover_system_install(*, candidate: VerifiedStagedCandidate, transaction_root: Path, installation_root: Path | None = None) -> SystemUpdateResult:
    journal = _load(transaction_root, candidate)
    if journal is None: return SystemUpdateResult(SystemUpdateStatus.BLOCKED, SystemUpdateCode.JOURNAL_INVALID)
    if journal["state"] == _COMPLETED:
        _retire_if_safe(transaction_root)
        return SystemUpdateResult(SystemUpdateStatus.INSTALLED)
    if journal["state"] in {_ROLLBACK_VERIFIED, _MANUAL}:
        if journal["state"] == _ROLLBACK_VERIFIED:
            _retire_if_safe(transaction_root)
            return SystemUpdateResult(SystemUpdateStatus.ROLLED_BACK)
        return SystemUpdateResult(SystemUpdateStatus.BLOCKED, SystemUpdateCode.MANUAL_RECOVERY)
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
                old_mode = _rollback_mode(item, backup)
                _replace_file(backup, target, old_mode)
            if _state(target) != item["old"] or (item["old"] is not None and _mode(target) != old_mode): raise ValueError
            item["checkpoint"] = "rollback_verified"; _save(transaction_root, journal)
        journal["state"] = _ROLLBACK_VERIFIED; _save(transaction_root, journal)
        _retire_if_safe(transaction_root)
        return SystemUpdateResult(SystemUpdateStatus.ROLLED_BACK)
    except (OSError, ValueError, KeyError):
        journal["state"] = _MANUAL; _save(transaction_root, journal)
        return SystemUpdateResult(SystemUpdateStatus.BLOCKED, SystemUpdateCode.MANUAL_RECOVERY)


def _recover_pending_journal(root: Path, journal: dict, installation_root: Path) -> SystemUpdateResult:
    """Roll back one already-validated incomplete journal."""
    try:
        state = journal["state"]
        files = journal["files"]
        if state == _MANUAL:
            return SystemUpdateResult(SystemUpdateStatus.BLOCKED, SystemUpdateCode.MANUAL_RECOVERY)
        if state not in _RECOVERABLE_STATES:
            return SystemUpdateResult(SystemUpdateStatus.BLOCKED, SystemUpdateCode.JOURNAL_INVALID)
        journal["state"] = _ROLLBACK_STARTED; _save(root, journal)
        for item in reversed(files):
            path = item.get("path")
            if not isinstance(path, str) or not _safe_path(path):
                raise ValueError
            target = Path(installation_root) / path
            state_now = _state(target)
            old, new = item.get("old"), item.get("new")
            if state_now == old:
                continue
            if state_now != new:
                raise ValueError
            if old is None:
                target.unlink()
            else:
                rollback = item.get("rollback")
                if not isinstance(rollback, str) or not _safe_path(rollback):
                    raise ValueError
                backup = root / rollback
                if _state(backup) != old:
                    raise ValueError
                old_mode = _rollback_mode(item, backup)
                _replace_file(backup, target, old_mode)
            if _state(target) != old or (old is not None and _mode(target) != old_mode):
                raise ValueError
            item["checkpoint"] = "rollback_verified"; _save(root, journal)
        journal["state"] = _ROLLBACK_VERIFIED; _save(root, journal)
        _retire_if_safe(root)
        return SystemUpdateResult(SystemUpdateStatus.ROLLED_BACK)
    except (OSError, ValueError, KeyError, TypeError):
        try:
            journal["state"] = _MANUAL; _save(root, journal)
        except (OSError, UnboundLocalError):
            pass
        return SystemUpdateResult(SystemUpdateStatus.BLOCKED, SystemUpdateCode.MANUAL_RECOVERY)


def recover_pending_system_installs(*, transaction_parent: Path, installation_root: Path) -> SystemUpdateResult | None:
    """Validate every transaction, retire terminal ones, then recover one.

    Startup deliberately runs before manifest discovery.  Recovery therefore
    uses only durable journals and rollback artifacts, never fetched metadata.
    """
    parent = Path(transaction_parent)
    try:
        parent_stat = os.stat(parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(parent_stat.st_mode):
        return SystemUpdateResult(SystemUpdateStatus.BLOCKED, SystemUpdateCode.JOURNAL_INVALID)

    validated: list[tuple[Path, dict]] = []
    try:
        entries = sorted(os.scandir(parent), key=lambda entry: entry.name)
        for entry in entries:
            root = parent / entry.name
            root_stat = os.stat(root, follow_symlinks=False)
            journal_path = _journal_path(root)
            if stat.S_ISLNK(root_stat.st_mode):
                if os.path.lexists(journal_path):
                    return SystemUpdateResult(SystemUpdateStatus.BLOCKED, SystemUpdateCode.JOURNAL_INVALID)
                continue
            if not stat.S_ISDIR(root_stat.st_mode) or not os.path.lexists(journal_path):
                continue
            if not _is_safe_transaction_root(root, parent) or not _is_regular(journal_path):
                return SystemUpdateResult(SystemUpdateStatus.BLOCKED, SystemUpdateCode.JOURNAL_INVALID)
            journal = json.loads(journal_path.read_text("utf-8"))
            if not _valid_journal(journal):
                return SystemUpdateResult(SystemUpdateStatus.BLOCKED, SystemUpdateCode.JOURNAL_INVALID)
            validated.append((root, journal))
    except (OSError, ValueError, TypeError):
        return SystemUpdateResult(SystemUpdateStatus.BLOCKED, SystemUpdateCode.JOURNAL_INVALID)

    terminal = [(root, journal) for root, journal in validated if journal["state"] in _TERMINAL_STATES]
    unfinished = [(root, journal) for root, journal in validated if journal["state"] not in _TERMINAL_STATES]
    if any(journal["state"] == _MANUAL for _, journal in unfinished):
        return SystemUpdateResult(SystemUpdateStatus.BLOCKED, SystemUpdateCode.MANUAL_RECOVERY)
    if len(unfinished) > 1:
        return SystemUpdateResult(SystemUpdateStatus.BLOCKED, SystemUpdateCode.JOURNAL_INVALID)
    for root, _ in terminal:
        if not _retire_transaction(root, parent):
            return SystemUpdateResult(SystemUpdateStatus.BLOCKED, SystemUpdateCode.JOURNAL_INVALID)
    if not unfinished:
        return None
    root, journal = unfinished[0]
    return _recover_pending_journal(root, journal, Path(installation_root))
