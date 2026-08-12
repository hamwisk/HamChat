from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hamchat.update_assessment import PreservationClass
from hamchat.legacy_registry_migration import LegacyMigrationPlan, MigrationStatus, RegistryFamily
from hamchat.update_preservation import (
    BackupFailureCode,
    BackupTransaction,
    JournalState,
    PreservationRequest,
    PreservationSourceKind,
    create_verified_backup,
    CapturedArtifact,
    load_journal,
    verify_bundle,
)


def transaction(tmp_path: Path, requests=(), transaction_id="transaction-0001"):
    source = tmp_path / "source checkout"
    source.mkdir(exist_ok=True)
    return BackupTransaction(
        transaction_id=transaction_id,
        source_root=source,
        data_root=tmp_path / "external data",
        preservation_root=tmp_path / "preservation",
        installed_commit="a" * 40,
        target_commit="b" * 40,
        assessment_digest="c" * 64,
        plan_digest="d" * 64,
        requests=tuple(requests),
    )


def request(path="settings/app.json", backup="files/settings/app.json"):
    return PreservationRequest(
        logical_id="settings-app", source_root=Path("unused"),
        relative_path=Path(path), backup_path=Path(backup),
        classification=PreservationClass.USER_OWNED,
    )


def with_source(tx, *requests):
    return BackupTransaction(**{**tx.__dict__, "requests": tuple(
        PreservationRequest(**{**item.__dict__, "source_root": tx.source_root})
        for item in requests
    )})


@pytest.mark.parametrize("root_name", ["one", "unrelated path", "checkout with spaces"])
def test_verified_backup_preserves_exact_bytes_independent_of_cwd(tmp_path, monkeypatch, root_name):
    outer = tmp_path / root_name; outer.mkdir()
    tx = transaction(outer)
    source = tx.source_root / "settings"; source.mkdir()
    payload = b"\x00exact\xffbytes\n"
    original = source / "app.json"; original.write_bytes(payload)
    tx = with_source(tx, request())
    monkeypatch.chdir(tmp_path)
    result = create_verified_backup(tx)
    assert result.verified and not result.source_mutation_permitted
    assert (result.bundle_path / "files/settings/app.json").read_bytes() == payload
    assert original.read_bytes() == payload
    assert load_journal(result.journal_path)["state"] == JournalState.BACKUP_VERIFIED.value


def test_zero_length_and_streamed_file_have_digests(tmp_path):
    tx = transaction(tmp_path)
    (tx.source_root / "empty").write_bytes(b"")
    (tx.source_root / "large").write_bytes(b"x" * (2 * 1024 * 1024 + 17))
    requests = [request("empty", "files/empty"), PreservationRequest("large", tx.source_root, Path("large"), Path("files/large"), PreservationClass.USER_OWNED)]
    result = create_verified_backup(with_source(tx, *requests))
    assert result.verified and [item.size for item in result.artifacts] == [0, 2 * 1024 * 1024 + 17]


@pytest.mark.parametrize("mutate", ["missing", "directory", "symlink", "escape"])
def test_unsafe_sources_are_rejected(tmp_path, mutate):
    tx = transaction(tmp_path)
    if mutate == "directory": (tx.source_root / "x").mkdir()
    elif mutate == "symlink":
        target = tx.source_root / "target"; target.write_text("x"); (tx.source_root / "x").symlink_to(target)
    elif mutate == "escape":
        (tx.source_root / "x").write_text("x")
    path = "../x" if mutate == "escape" else "x"
    result = create_verified_backup(with_source(tx, request(path)))
    assert not result.verified
    assert result.failure.code in {BackupFailureCode.SOURCE_MISSING, BackupFailureCode.SOURCE_TYPE_UNSUPPORTED, BackupFailureCode.PATH_ESCAPE}


@pytest.mark.parametrize("field,value,code", [
    ("transaction_id", "bad/slash", BackupFailureCode.INVALID_TRANSACTION_ID),
    ("installed_commit", "bad", BackupFailureCode.INVALID_INSTALLED_IDENTITY),
    ("target_commit", "bad", BackupFailureCode.INVALID_TARGET_IDENTITY),
])
def test_identity_validation(tmp_path, field, value, code):
    tx = transaction(tmp_path)
    values = {**tx.__dict__, field: value}
    result = create_verified_backup(BackupTransaction(**values))
    assert result.failure.code is code


def test_limits_duplicate_paths_and_backup_root_overlap_block(tmp_path):
    tx = transaction(tmp_path)
    (tx.source_root / "x").write_bytes(b"1234")
    first = PreservationRequest("one", tx.source_root, Path("x"), Path("f"), PreservationClass.USER_OWNED)
    duplicate = PreservationRequest("two", tx.source_root, Path("x"), Path("f"), PreservationClass.USER_OWNED)
    assert create_verified_backup(with_source(tx, first, duplicate)).failure.code is BackupFailureCode.UNEXPECTED_PATH
    limited = BackupTransaction(**{**with_source(tx, first).__dict__, "max_file_bytes": 1})
    assert create_verified_backup(limited).failure.code is BackupFailureCode.SOURCE_TOO_LARGE
    overlap = BackupTransaction(**{**tx.__dict__, "preservation_root": tx.source_root / "preservation"})
    assert create_verified_backup(overlap).failure.code is BackupFailureCode.BACKUP_ROOT_OVERLAP


def test_manifest_corruption_and_matching_verified_retry(tmp_path):
    tx = transaction(tmp_path)
    (tx.source_root / "x").write_bytes(b"x")
    tx = with_source(tx, PreservationRequest("x", tx.source_root, Path("x"), Path("files/x"), PreservationClass.USER_OWNED))
    first = create_verified_backup(tx)
    second = create_verified_backup(tx)
    assert second.verified and second.bundle_path == first.bundle_path
    (first.bundle_path / "files/x").write_bytes(b"changed")
    third = create_verified_backup(tx)
    assert not third.verified and third.failure.code is BackupFailureCode.BUNDLE_CORRUPTED


def test_manifest_and_journal_reject_corruption_and_future_versions(tmp_path):
    tx = transaction(tmp_path)
    (tx.source_root / "x").write_bytes(b"x")
    result = create_verified_backup(with_source(tx, PreservationRequest("x", tx.source_root, Path("x"), Path("files/x"), PreservationClass.USER_OWNED)))
    manifest = result.bundle_path / "manifest.json"
    raw = json.loads(manifest.read_text()); raw["format_version"] = 2; manifest.write_text(json.dumps(raw))
    assert verify_bundle(result.bundle_path)[1].code is BackupFailureCode.MANIFEST_INVALID
    result.journal_path.write_text("{")
    with pytest.raises(ValueError): load_journal(result.journal_path)


def test_manifest_verified_flag_is_integrity_bound(tmp_path):
    tx = transaction(tmp_path)
    (tx.source_root / "x").write_bytes(b"x")
    result = create_verified_backup(with_source(tx, PreservationRequest("x", tx.source_root, Path("x"), Path("files/x"), PreservationClass.USER_OWNED)))
    manifest = result.bundle_path / "manifest.json"
    raw = json.loads(manifest.read_text()); raw["verified"] = False; manifest.write_text(json.dumps(raw))
    assert verify_bundle(result.bundle_path)[1].code in {BackupFailureCode.DIGEST_MISMATCH, BackupFailureCode.MANIFEST_INVALID}


def test_ready_legacy_plan_captures_originals_without_executing_migration(tmp_path):
    tx = transaction(tmp_path)
    settings = tx.source_root / "settings"; settings.mkdir()
    tracked = settings / "context_overrides.json"; tracked.write_bytes(b'{"legacy":true}')
    user = settings / "context_overrides.user.json"; user.write_bytes(b'{"version":1}')
    plan = LegacyMigrationPlan(RegistryFamily.CONTEXT, MigrationStatus.READY, "a" * 40, "b" * 40, (), {"version": 1}, "e" * 64, (), ("settings/context_overrides.json", "settings/context_overrides.user.json"), True)
    result = create_verified_backup(BackupTransaction(**{**tx.__dict__, "legacy_migration_plans": (plan,)}))
    assert result.verified
    assert (result.bundle_path / "legacy/settings/context_overrides.json").read_bytes() == tracked.read_bytes()
    assert user.read_bytes() == b'{"version":1}'
    assert not (settings / "modality_triggers.user.json").exists()


@pytest.mark.parametrize("status", [MigrationStatus.BLOCKED, MigrationStatus.NOT_REQUIRED])
def test_non_ready_legacy_plans_do_not_become_migrations(tmp_path, status):
    tx = transaction(tmp_path)
    plan = LegacyMigrationPlan(RegistryFamily.CONTEXT, status, "a" * 40, "b" * 40, (), None, None, (), (), status is MigrationStatus.BLOCKED)
    result = create_verified_backup(BackupTransaction(**{**tx.__dict__, "legacy_migration_plans": (plan,)}))
    if status is MigrationStatus.BLOCKED:
        assert result.failure.code is BackupFailureCode.PLAN_MISMATCH
    else:
        assert result.verified and not result.artifacts


def test_data_snapshot_never_uses_live_database_copy_fallback(tmp_path):
    tx = transaction(tmp_path)
    result = create_verified_backup(with_source(tx, PreservationRequest("data", tx.data_root, Path("."), Path("data"), PreservationClass.EXTERNAL_AUTHORITATIVE_STATE, PreservationSourceKind.DATA_SNAPSHOT)))
    assert result.failure.code is BackupFailureCode.DATABASE_SNAPSHOT_UNAVAILABLE
    assert not tx.data_root.exists()


def test_injected_snapshot_provider_is_the_only_data_root_capture_path(tmp_path):
    tx = transaction(tmp_path)
    request_data = PreservationRequest("data", tx.data_root, Path("."), Path("data"), PreservationClass.EXTERNAL_AUTHORITATIVE_STATE, PreservationSourceKind.DATA_SNAPSHOT)

    class Provider:
        def capture(self, source_root, destination):
            destination.mkdir(parents=True)
            content = b"sqlite snapshot, not source db copy"
            path = destination / "db.sqlite"
            path.write_bytes(content)
            digest = __import__("hashlib").sha256(content).hexdigest()
            return (CapturedArtifact("database", "data/db.sqlite", "external_authoritative_state", "injected_snapshot", len(content), digest, digest, True),)

    result = create_verified_backup(with_source(tx, request_data), snapshot_provider=Provider())
    assert result.verified
    assert (result.bundle_path / "data/db.sqlite").read_bytes().startswith(b"sqlite snapshot")


def test_aborted_journal_never_reports_verified_and_does_not_create_registry(tmp_path):
    tx = transaction(tmp_path)
    result = create_verified_backup(with_source(tx, request("missing-registry")))
    assert not result.verified and result.journal_state is JournalState.ABORTED
    assert load_journal(result.journal_path)["state"] == JournalState.ABORTED.value
    assert not (tx.source_root / "settings/context_overrides.user.json").exists()
