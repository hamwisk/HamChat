"""Read-only installation safety assessment for a future updater.

No function in this module mutates Git, the installation, backups, or state.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Mapping, Protocol, Sequence
from urllib.parse import urlsplit


EXPECTED_REMOTES = frozenset({
    "git@github.com:hamwisk/HamChat.git",
    "https://github.com/hamwisk/HamChat.git",
})
READ_ONLY_GIT_COMMANDS = frozenset({
    "rev-parse", "config", "status", "ls-tree", "show", "submodule",
})
LEGACY_REGISTRIES = frozenset({
    "settings/context_overrides.json", "settings/modality_triggers.json",
})


class AssessmentReason(str, Enum):
    MISSING_REPOSITORY = "missing_repository"
    NON_GIT_INSTALLATION = "non_git_installation"
    BARE_REPOSITORY = "bare_repository"
    UNUSUAL_WORKTREE = "unusual_worktree"
    SUBMODULES_PRESENT = "submodules_present"
    UNEXPECTED_REMOTE = "unexpected_remote"
    GIT_TIMEOUT = "git_timeout"
    GIT_FAILURE = "git_failure"
    MALFORMED_GIT_OUTPUT = "malformed_git_output"
    TARGET_REF_UNAVAILABLE = "target_ref_unavailable"
    TRACKED_CHANGE = "tracked_change"
    LEGACY_REGISTRY_CUSTOMIZATION = "legacy_registry_customization"
    UNTRACKED_TARGET_COLLISION = "untracked_target_collision"
    UNKNOWN_UNTRACKED_PATH = "unknown_untracked_path"
    PATH_ESCAPE = "path_escape"
    DATA_DIRECTORY_INACCESSIBLE = "data_directory_inaccessible"
    BACKUP_DESTINATION_UNSAFE = "backup_destination_unsafe"
    BACKUP_DESTINATION_EXISTS = "backup_destination_exists"
    BACKUP_DESTINATION_UNWRITABLE = "backup_destination_unwritable"
    INSUFFICIENT_BACKUP_SPACE = "insufficient_backup_space"
    INDETERMINATE_BACKUP_SPACE = "indeterminate_backup_space"


class PreservationClass(str, Enum):
    RELEASE_OWNED = "release_owned"
    USER_OWNED = "user_owned"
    USER_EXTENSION = "user_extension"
    LEGACY_TRACKED_CUSTOMIZATION = "legacy_tracked_customization"
    DERIVED_OR_DISPOSABLE = "derived_or_disposable"
    AMBIGUOUS_OR_CONFLICTING = "ambiguous_or_conflicting"
    EXTERNAL_AUTHORITATIVE_STATE = "external_authoritative_state"


class RepositoryKind(str, Enum):
    SUPPORTED_GIT_CHECKOUT = "supported_git_checkout"
    MISSING = "missing"
    NON_GIT = "non_git"
    BARE = "bare"
    UNUSUAL_WORKTREE = "unusual_worktree"


class LegacyMigrationAssessment(str, Enum):
    NOT_REQUIRED = "not_required"
    POTENTIALLY_MIGRATABLE = "potentially_migratable"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class LegacyMigrationFinding:
    """Read-only legacy-registry planning state retained with an assessment.

    This deliberately never alters ``reasons``: a potentially migratable
    tracked edit still blocks a future mutating updater until a later
    backup-and-execution transaction exists.
    """

    registry_path: str
    status: LegacyMigrationAssessment


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class GitRunner(Protocol):
    def run(self, args: Sequence[str], *, cwd: Path, timeout: float) -> CommandResult: ...


class SubprocessGitRunner:
    """Bounded argument-array implementation restricted to read-only commands."""

    def run(self, args: Sequence[str], *, cwd: Path, timeout: float) -> CommandResult:
        if not args or args[0] not in READ_ONLY_GIT_COMMANDS:
            raise ValueError("non-read-only Git command rejected")
        try:
            completed = subprocess.run(
                ["git", *args], cwd=cwd, text=True, capture_output=True,
                timeout=timeout, check=False, shell=False,
                env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C"},
            )
            return CommandResult(completed.returncode, completed.stdout[:65536], completed.stderr[:4096])
        except subprocess.TimeoutExpired:
            return CommandResult(1, timed_out=True)
        except OSError:
            return CommandResult(1)


@dataclass(frozen=True)
class PathFinding:
    path: Path
    preservation: PreservationClass
    reason: AssessmentReason | None = None


@dataclass(frozen=True)
class InstallationAssessment:
    repository_kind: RepositoryKind
    installation_root: Path
    remote: str | None
    installed_commit: str | None
    target_ref: str | None
    target_commit: str | None
    data_dir: Path
    data_dir_external: bool
    findings: tuple[PathFinding, ...]
    reasons: tuple[AssessmentReason, ...]
    legacy_migrations: tuple[LegacyMigrationFinding, ...] = ()

    @property
    def safe_for_future_backup(self) -> bool:
        return not self.reasons


@dataclass(frozen=True)
class PreservationPlan:
    findings: tuple[PathFinding, ...]
    future_phases: tuple[str, ...]
    blocked: bool


def summarize_legacy_migration(plan: object) -> LegacyMigrationAssessment:
    """Expose a pure planner result without clearing any assessment blocker."""
    status = getattr(plan, "status", None)
    value = getattr(status, "value", status)
    if value == "ready":
        return LegacyMigrationAssessment.POTENTIALLY_MIGRATABLE
    if value == "not_required":
        return LegacyMigrationAssessment.NOT_REQUIRED
    return LegacyMigrationAssessment.BLOCKED


def with_legacy_migration(
    assessment: InstallationAssessment, registry_path: str, plan: object,
) -> InstallationAssessment:
    """Return an assessment annotated with a non-authorizing planner result.

    Only the two known tracked registry paths are accepted.  The returned
    assessment preserves every dirty-worktree blocker from the original.
    """
    if registry_path not in LEGACY_REGISTRIES:
        raise ValueError("unknown legacy registry path")
    finding = LegacyMigrationFinding(
        registry_path, summarize_legacy_migration(plan),
    )
    retained = tuple(
        item for item in assessment.legacy_migrations
        if item.registry_path != registry_path
    )
    return replace(assessment, legacy_migrations=retained + (finding,))


def normalize_hamchat_remote(value: object) -> str | None:
    """Accept only the two documented GitHub SSH/HTTPS identity forms."""
    if not isinstance(value, str) or value != value.strip() or not value:
        return None
    if value == "git@github.com:hamwisk/HamChat.git":
        return value
    parsed = urlsplit(value)
    if (
        parsed.scheme == "https" and parsed.hostname == "github.com"
        and parsed.port is None and parsed.username is None and parsed.password is None
        and parsed.path == "/hamwisk/HamChat.git" and not parsed.query and not parsed.fragment
    ):
        return "https://github.com/hamwisk/HamChat.git"
    return None


def resolve_data_dir(installation_root: Path, *, cli_data_dir: str | None, environ: Mapping[str, str]) -> Path:
    raw = cli_data_dir if cli_data_dir is not None else environ.get("HAMCHAT_DATA_DIR")
    return Path(raw).expanduser().resolve() if raw else (installation_root / "data").resolve()


def _git(runner: GitRunner, root: Path, args: Sequence[str], timeout: float) -> CommandResult:
    result = runner.run(args, cwd=root, timeout=timeout)
    return result


def _commit(value: str) -> str | None:
    value = value.strip()
    return value if re.fullmatch(r"[0-9a-f]{40}", value) else None


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _classify_path(path: str, status: str, target_paths: frozenset[str]) -> PathFinding:
    normalized = path.rstrip("/")
    if normalized == "data" or normalized.startswith("data/"):
        return PathFinding(Path(path), PreservationClass.USER_OWNED)
    if normalized in {"settings/app.json", "settings/models.json"}:
        return PathFinding(Path(path), PreservationClass.USER_OWNED)
    if normalized in {"settings/context_overrides.user.json", "settings/modality_triggers.user.json"}:
        return PathFinding(Path(path), PreservationClass.USER_OWNED)
    if normalized.startswith("settings/themes/") and normalized != "settings/themes/default_theme.json":
        return PathFinding(Path(path), PreservationClass.USER_EXTENSION)
    if status != "??":
        if normalized in LEGACY_REGISTRIES:
            return PathFinding(Path(path), PreservationClass.LEGACY_TRACKED_CUSTOMIZATION, AssessmentReason.LEGACY_REGISTRY_CUSTOMIZATION)
        return PathFinding(Path(path), PreservationClass.AMBIGUOUS_OR_CONFLICTING, AssessmentReason.TRACKED_CHANGE)
    if normalized in target_paths or any(item.startswith(normalized + "/") for item in target_paths):
        return PathFinding(Path(path), PreservationClass.AMBIGUOUS_OR_CONFLICTING, AssessmentReason.UNTRACKED_TARGET_COLLISION)
    return PathFinding(Path(path), PreservationClass.AMBIGUOUS_OR_CONFLICTING, AssessmentReason.UNKNOWN_UNTRACKED_PATH)


def _parse_porcelain(text: str) -> list[tuple[str, str]] | None:
    entries: list[tuple[str, str]] = []
    for record in filter(None, text.split("\0")):
        if len(record) < 4 or record[2] != " ":
            return None
        status, path = record[:2], record[3:]
        if status == "!!":
            entries.append((status, path))
        elif status == "??" or status.strip():
            entries.append((status, path))
    return entries


def assess_installation(
    installation_root: Path,
    *,
    target_ref: str | None,
    runner: GitRunner,
    cli_data_dir: str | None = None,
    environ: Mapping[str, str] | None = None,
    backup_destination: Path | None = None,
    timeout: float = 5.0,
) -> InstallationAssessment:
    """Inspect only; it never accesses a database or writes filesystem state."""
    root_was_symlink = installation_root.is_symlink()
    root = installation_root.resolve()
    env = environ or {}
    findings: list[PathFinding] = []
    reasons: list[AssessmentReason] = []
    data_dir = resolve_data_dir(root, cli_data_dir=cli_data_dir, environ=env)
    external = not _within(data_dir, root)
    if root_was_symlink:
        reasons.append(AssessmentReason.PATH_ESCAPE)
    if not root.exists():
        return InstallationAssessment(RepositoryKind.MISSING, root, None, None, target_ref, None, data_dir, external, (), (AssessmentReason.MISSING_REPOSITORY,))
    worktree = _git(runner, root, ("rev-parse", "--is-inside-work-tree"), timeout)
    if worktree.timed_out:
        return InstallationAssessment(RepositoryKind.NON_GIT, root, None, None, target_ref, None, data_dir, external, (), (AssessmentReason.GIT_TIMEOUT,))
    if worktree.returncode != 0:
        return InstallationAssessment(RepositoryKind.NON_GIT, root, None, None, target_ref, None, data_dir, external, (), (AssessmentReason.NON_GIT_INSTALLATION,))
    bare = _git(runner, root, ("rev-parse", "--is-bare-repository"), timeout)
    if bare.stdout.strip() == "true":
        return InstallationAssessment(RepositoryKind.BARE, root, None, None, target_ref, None, data_dir, external, (), (AssessmentReason.BARE_REPOSITORY,))
    top = _git(runner, root, ("rev-parse", "--show-toplevel"), timeout)
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != root:
        reasons.append(AssessmentReason.UNUSUAL_WORKTREE)
    git_dir = _git(runner, root, ("rev-parse", "--git-dir"), timeout)
    if git_dir.returncode != 0 or git_dir.stdout.strip() != ".git":
        reasons.append(AssessmentReason.UNUSUAL_WORKTREE)
    remote_result = _git(runner, root, ("config", "--get", "remote.origin.url"), timeout)
    remote = remote_result.stdout.strip() if remote_result.returncode == 0 else None
    if normalize_hamchat_remote(remote) is None:
        reasons.append(AssessmentReason.UNEXPECTED_REMOTE)
    submodules = _git(runner, root, ("submodule", "status", "--recursive"), timeout)
    if submodules.returncode == 0 and submodules.stdout.strip():
        reasons.append(AssessmentReason.SUBMODULES_PRESENT)
    head = _git(runner, root, ("rev-parse", "HEAD"), timeout)
    installed = _commit(head.stdout) if head.returncode == 0 else None
    if installed is None:
        reasons.append(AssessmentReason.MALFORMED_GIT_OUTPUT)
    target_commit = None
    target_paths: frozenset[str] = frozenset()
    if target_ref:
        resolved = _git(runner, root, ("rev-parse", "--verify", f"{target_ref}^{{commit}}"), timeout)
        target_commit = _commit(resolved.stdout) if resolved.returncode == 0 else None
        if target_commit is None:
            reasons.append(AssessmentReason.TARGET_REF_UNAVAILABLE)
        else:
            tree = _git(runner, root, ("ls-tree", "-r", "--name-only", target_commit), timeout)
            if tree.returncode != 0:
                reasons.append(AssessmentReason.GIT_FAILURE)
            else:
                target_paths = frozenset(line for line in tree.stdout.splitlines() if line)
    status = _git(runner, root, ("status", "--porcelain=v1", "-z", "--ignored=matching"), timeout)
    parsed = _parse_porcelain(status.stdout) if status.returncode == 0 else None
    if parsed is None:
        reasons.append(AssessmentReason.MALFORMED_GIT_OUTPUT if status.returncode == 0 else AssessmentReason.GIT_FAILURE)
    else:
        for code, path in parsed:
            if code == "!!":
                continue
            finding = _classify_path(path, code, target_paths)
            findings.append(finding)
            if finding.reason is not None:
                reasons.append(finding.reason)
    if data_dir.exists() and not os.access(data_dir, os.R_OK | os.W_OK):
        reasons.append(AssessmentReason.DATA_DIRECTORY_INACCESSIBLE)
    if backup_destination is not None:
        destination = backup_destination.resolve()
        if _within(destination, root) or _within(destination, data_dir):
            reasons.append(AssessmentReason.BACKUP_DESTINATION_UNSAFE)
        elif destination.exists():
            reasons.append(AssessmentReason.BACKUP_DESTINATION_EXISTS)
        elif not os.access(destination.parent, os.W_OK):
            reasons.append(AssessmentReason.BACKUP_DESTINATION_UNWRITABLE)
        else:
            try:
                free = shutil.disk_usage(destination.parent).free
                if free <= 0:
                    reasons.append(AssessmentReason.INSUFFICIENT_BACKUP_SPACE)
            except OSError:
                reasons.append(AssessmentReason.INDETERMINATE_BACKUP_SPACE)
    if any(result.timed_out for result in (bare, top, git_dir, remote_result, submodules, head, status)):
        reasons.append(AssessmentReason.GIT_TIMEOUT)
    return InstallationAssessment(
        RepositoryKind.SUPPORTED_GIT_CHECKOUT if not reasons else RepositoryKind.UNUSUAL_WORKTREE if AssessmentReason.UNUSUAL_WORKTREE in reasons else RepositoryKind.SUPPORTED_GIT_CHECKOUT,
        root, remote, installed, target_ref, target_commit, data_dir, external,
        tuple(sorted(findings, key=lambda item: item.path.as_posix())), tuple(sorted(set(reasons), key=lambda item: item.value)),
    )


def build_preservation_plan(assessment: InstallationAssessment) -> PreservationPlan:
    """Purely describe later phases; never creates a backup or update marker."""
    findings = list(assessment.findings)
    data_class = PreservationClass.EXTERNAL_AUTHORITATIVE_STATE if assessment.data_dir_external else PreservationClass.USER_OWNED
    findings.append(PathFinding(assessment.data_dir, data_class))
    findings.append(
        PathFinding(
            assessment.installation_root / "settings/themes/default_theme.json",
            PreservationClass.RELEASE_OWNED,
        )
    )
    for name in ("context_overrides.json", "modality_triggers.json"):
        findings.append(PathFinding(assessment.installation_root / "settings" / name, PreservationClass.RELEASE_OWNED))
    for name in ("context_overrides.user.json", "modality_triggers.user.json"):
        findings.append(PathFinding(assessment.installation_root / "settings" / name, PreservationClass.USER_OWNED))
    return PreservationPlan(
        tuple(sorted(findings, key=lambda item: item.path.as_posix())),
        ("backup", "quiesce_database", "verify_target", "apply", "migrate", "restart") if not assessment.reasons else (),
        bool(assessment.reasons),
    )
