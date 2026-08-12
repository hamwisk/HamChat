from __future__ import annotations

from pathlib import Path

import pytest

from hamchat.update_assessment import (
    AssessmentReason, CommandResult, PreservationClass, RepositoryKind,
    LegacyMigrationAssessment, assess_installation, build_preservation_plan, normalize_hamchat_remote,
    resolve_data_dir,
    with_legacy_migration,
)


@pytest.mark.parametrize(("status", "expected"), [
    ("not_required", LegacyMigrationAssessment.NOT_REQUIRED),
    ("ready", LegacyMigrationAssessment.POTENTIALLY_MIGRATABLE),
    ("blocked", LegacyMigrationAssessment.BLOCKED),
])
def test_legacy_migration_assessment_is_non_authorizing(tmp_path, status, expected):
    root = tmp_path / "root"; root.mkdir()
    assessment = assess_installation(
        root, target_ref="v2.7.0",
        runner=RecordingRunner(clean_replies(root, status=" M settings/context_overrides.json\0")),
    )
    plan = type("Plan", (), {"status": status})()
    annotated = with_legacy_migration(assessment, "settings/context_overrides.json", plan)
    assert annotated.legacy_migrations[0].status is expected
    assert annotated.reasons == assessment.reasons
    assert not annotated.safe_for_future_backup


class RecordingRunner:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def run(self, args, *, cwd, timeout):
        self.calls.append(tuple(args))
        return self.replies.get(tuple(args), CommandResult(1))


def clean_replies(root, *, status="", target=True):
    replies = {
        ("rev-parse", "--is-inside-work-tree"): CommandResult(0, "true\n"),
        ("rev-parse", "--is-bare-repository"): CommandResult(0, "false\n"),
        ("rev-parse", "--show-toplevel"): CommandResult(0, str(root) + "\n"),
        ("rev-parse", "--git-dir"): CommandResult(0, ".git\n"),
        ("config", "--get", "remote.origin.url"): CommandResult(0, "git@github.com:hamwisk/HamChat.git\n"),
        ("submodule", "status", "--recursive"): CommandResult(0, ""),
        ("rev-parse", "HEAD"): CommandResult(0, "a" * 40 + "\n"),
        ("status", "--porcelain=v1", "-z", "--ignored=matching"): CommandResult(0, status),
    }
    if target:
        replies[("rev-parse", "--verify", "v2.7.0^{commit}")] = CommandResult(0, "b" * 40 + "\n")
        replies[("ls-tree", "-r", "--name-only", "b" * 40)] = CommandResult(0, "main.py\nsettings/context_overrides.json\n")
    return replies


@pytest.mark.parametrize("remote", ["git@github.com:hamwisk/HamChat.git", "https://github.com/hamwisk/HamChat.git"])
def test_accepts_exact_remote_forms(remote):
    assert normalize_hamchat_remote(remote) is not None


@pytest.mark.parametrize("remote", ["https://evil.example/hamwisk/HamChat.git", "https://github.com.evil.example/hamwisk/HamChat.git", "git@github.com:attacker/HamChat.git", "https://github.com/hamwisk/HamChat.git@evil.example/repo", "https://user@github.com/hamwisk/HamChat.git", "https://github.com/hamwisk/HamChat.git?q=1"])
def test_rejects_deceptive_remotes(remote):
    assert normalize_hamchat_remote(remote) is None


def test_clean_checkout_resolves_local_target_and_uses_read_only_commands(tmp_path):
    root = tmp_path / "install"; root.mkdir()
    runner = RecordingRunner(clean_replies(root))
    result = assess_installation(root, target_ref="v2.7.0", runner=runner)
    assert result.repository_kind is RepositoryKind.SUPPORTED_GIT_CHECKOUT
    assert result.target_commit == "b" * 40
    assert result.safe_for_future_backup
    assert all(call[0] in {"rev-parse", "config", "status", "ls-tree", "submodule"} for call in runner.calls)


@pytest.mark.parametrize(("status", "reason"), [
    (" M main.py\0", AssessmentReason.TRACKED_CHANGE),
    (" M settings/context_overrides.json\0", AssessmentReason.LEGACY_REGISTRY_CUSTOMIZATION),
    ("M  main.py\0", AssessmentReason.TRACKED_CHANGE),
    (" D main.py\0", AssessmentReason.TRACKED_CHANGE),
    ("?? main.py\0", AssessmentReason.UNTRACKED_TARGET_COLLISION),
    ("?? note.txt\0", AssessmentReason.UNKNOWN_UNTRACKED_PATH),
])
def test_worktree_findings_block_without_treating_ignored_user_files_as_dirty(tmp_path, status, reason):
    root = tmp_path / "install"; root.mkdir()
    runner = RecordingRunner(clean_replies(root, status=status + "!! data/\0!! settings/app.json\0!! settings/models.json\0"))
    result = assess_installation(root, target_ref="v2.7.0", runner=runner)
    assert reason in result.reasons
    assert all(f.reason is not None for f in result.findings)


def test_custom_theme_is_a_user_extension(tmp_path):
    root = tmp_path / "install"; root.mkdir()
    runner = RecordingRunner(clean_replies(root, status="?? settings/themes/mine.json\0"))
    result = assess_installation(root, target_ref="v2.7.0", runner=runner)
    assert result.findings[0].preservation is PreservationClass.USER_EXTENSION
    assert result.reasons == ()


def test_tracked_default_theme_remains_release_owned_and_dirty_changes_block(tmp_path):
    root = tmp_path / "install"; root.mkdir()
    runner = RecordingRunner(clean_replies(root, status=" M settings/themes/default_theme.json\0"))
    result = assess_installation(root, target_ref="v2.7.0", runner=runner)
    assert AssessmentReason.TRACKED_CHANGE in result.reasons


def test_missing_non_git_bare_timeout_and_unresolved_target_are_controlled(tmp_path):
    missing = assess_installation(tmp_path / "missing", target_ref=None, runner=RecordingRunner({}))
    assert AssessmentReason.MISSING_REPOSITORY in missing.reasons
    root = tmp_path / "root"; root.mkdir()
    assert AssessmentReason.NON_GIT_INSTALLATION in assess_installation(root, target_ref=None, runner=RecordingRunner({})).reasons
    replies = clean_replies(root); replies[("rev-parse", "--is-bare-repository")] = CommandResult(0, "true\n")
    assert AssessmentReason.BARE_REPOSITORY in assess_installation(root, target_ref=None, runner=RecordingRunner(replies)).reasons
    replies = clean_replies(root, target=False)
    assert AssessmentReason.TARGET_REF_UNAVAILABLE in assess_installation(root, target_ref="v2.7.0", runner=RecordingRunner(replies)).reasons


def test_submodules_and_symlinked_roots_are_blocked(tmp_path):
    root = tmp_path / "root"; root.mkdir()
    replies = clean_replies(root); replies[("submodule", "status", "--recursive")] = CommandResult(0, " abc module\n")
    assert AssessmentReason.SUBMODULES_PRESENT in assess_installation(root, target_ref="v2.7.0", runner=RecordingRunner(replies)).reasons
    linked = tmp_path / "linked"; linked.symlink_to(root, target_is_directory=True)
    assert AssessmentReason.PATH_ESCAPE in assess_installation(linked, target_ref="v2.7.0", runner=RecordingRunner(clean_replies(root))).reasons


def test_linked_worktree_is_classified_as_unusual(tmp_path):
    root = tmp_path / "root"; root.mkdir()
    replies = clean_replies(root)
    replies[("rev-parse", "--git-dir")] = CommandResult(0, "/elsewhere/worktrees/root\n")
    result = assess_installation(root, target_ref="v2.7.0", runner=RecordingRunner(replies))
    assert result.repository_kind is RepositoryKind.UNUSUAL_WORKTREE
    assert AssessmentReason.UNUSUAL_WORKTREE in result.reasons


def test_data_resolution_precedence_and_pure_plan(tmp_path):
    root = tmp_path / "root"; root.mkdir()
    assert resolve_data_dir(root, cli_data_dir=None, environ={}) == (root / "data").resolve()
    assert resolve_data_dir(root, cli_data_dir=None, environ={"HAMCHAT_DATA_DIR": str(tmp_path / "env")}) == (tmp_path / "env").resolve()
    assert resolve_data_dir(root, cli_data_dir=str(tmp_path / "cli"), environ={"HAMCHAT_DATA_DIR": str(tmp_path / "env")}) == (tmp_path / "cli").resolve()
    runner = RecordingRunner(clean_replies(root))
    assessment = assess_installation(root, target_ref="v2.7.0", runner=runner, cli_data_dir=str(tmp_path / "outside"))
    plan = build_preservation_plan(assessment)
    assert not plan.blocked
    assert any(item.preservation is PreservationClass.EXTERNAL_AUTHORITATIVE_STATE for item in plan.findings)
    assert any(item.path.name == "default_theme.json" and item.preservation is PreservationClass.RELEASE_OWNED for item in plan.findings)
    assert {item.path.name for item in plan.findings if item.preservation is PreservationClass.USER_OWNED} >= {"context_overrides.user.json", "modality_triggers.user.json"}


def test_unsafe_or_existing_backup_destination_blocks(tmp_path):
    root = tmp_path / "root"; root.mkdir()
    inside = root / "backup"
    result = assess_installation(root, target_ref="v2.7.0", runner=RecordingRunner(clean_replies(root)), backup_destination=inside)
    assert AssessmentReason.BACKUP_DESTINATION_UNSAFE in result.reasons
    destination = tmp_path / "backup"; destination.mkdir()
    result = assess_installation(root, target_ref="v2.7.0", runner=RecordingRunner(clean_replies(root)), backup_destination=destination)
    assert AssessmentReason.BACKUP_DESTINATION_EXISTS in result.reasons
