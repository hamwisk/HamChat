from __future__ import annotations

import json

from hamchat.legacy_registry_migration import (
    MigrationCode, MigrationStatus, RegistryFamily, acquire_baseline,
    plan_legacy_registry_migration, plan_legacy_registry_migration_from_git,
)
from hamchat.infra.llm import registry_layers
from hamchat.update_assessment import CommandResult
from hamchat.update_assessment import LegacyMigrationAssessment, summarize_legacy_migration
import pytest


def context(): return {"version": 1, "by_name": {"model": 1024}, "by_regex": {"a.*": 2048}, "default_by_family": {"family": 4096}}
def modality(): return {"version": 1, "defaults_to": "text", "families_multimodal": ["a"], "name_contains_multimodal": ["vision"], "regex_multimodal": ["v.*"], "model_overrides": {"model": "multimodal"}}
def cu(): return {"version": 1, "by_name": {}, "by_regex": {}, "default_by_family": {}, "remove_by_name": [], "remove_by_regex": [], "remove_default_by_family": []}
def mu(): return {"version": 1, "add": {}, "remove": {}, "model_overrides": {}, "remove_model_overrides": []}


def test_context_add_replace_remove_and_target_conflict():
    local = context(); local["by_name"] = {"model": 8192, "new": 16384}; del local["by_regex"]["a.*"]
    plan = plan_legacy_registry_migration(RegistryFamily.CONTEXT, baseline=context(), local=local, target=context(), existing_user=None, installed_commit="a" * 40, target_identity="b" * 40)
    assert plan.status is MigrationStatus.READY and not plan.execution_permitted and plan.backup_required
    assert plan.projected_user_document["by_name"] == {"model": 8192, "new": 16384}
    assert plan.projected_user_document["remove_by_regex"] == ["a.*"]
    target = context(); target["by_name"]["model"] = 4096
    blocked = plan_legacy_registry_migration(RegistryFamily.CONTEXT, baseline=context(), local=local, target=target, existing_user=None, installed_commit="a" * 40, target_identity="b" * 40)
    assert blocked.blockers[0].code is MigrationCode.DIVERGENT_CHANGE


def test_context_reorder_and_existing_user_conflict_block():
    base = context(); base["by_regex"] = {"a.*": 1024, "b.*": 2048}
    local = context(); local["by_regex"] = {"b.*": 2048, "a.*": 1024}
    plan = plan_legacy_registry_migration(RegistryFamily.CONTEXT, baseline=base, local=local, target=base, existing_user=None, installed_commit="a" * 40, target_identity="b" * 40)
    assert plan.blockers[0].code is MigrationCode.UNREPRESENTABLE_ORDER
    local = context(); local["by_name"]["model"] = 8192
    user = cu(); user["by_name"]["model"] = 4096
    plan = plan_legacy_registry_migration(RegistryFamily.CONTEXT, baseline=context(), local=local, target=context(), existing_user=user, installed_commit="a" * 40, target_identity="b" * 40)
    assert plan.blockers[0].code is MigrationCode.USER_CONFLICT


def test_modality_add_remove_override_and_defaults_to_block():
    local = modality(); local["families_multimodal"].append("new"); local["name_contains_multimodal"] = []; local["model_overrides"]["model"] = "text"
    plan = plan_legacy_registry_migration(RegistryFamily.MODALITY, baseline=modality(), local=local, target=modality(), existing_user=None, installed_commit="a" * 40, target_identity="b" * 40)
    assert plan.status is MigrationStatus.READY
    assert plan.projected_user_document["add"]["families_multimodal"] == ["new"]
    assert plan.projected_user_document["remove"]["name_contains_multimodal"] == ["vision"]
    assert plan.projected_user_document["model_overrides"]["model"] == "text"
    local = modality(); local["defaults_to"] = "multimodal"
    assert plan_legacy_registry_migration(RegistryFamily.MODALITY, baseline=modality(), local=local, target=modality(), existing_user=None, installed_commit="a" * 40, target_identity="b" * 40).blockers[0].code is MigrationCode.RELEASE_OWNED_CHANGE


class Runner:
    def __init__(self, result): self.result, self.calls = result, []
    def run(self, args, *, cwd, timeout): self.calls.append(args); return self.result


def test_baseline_acquisition_is_fixed_path_read_only(tmp_path):
    runner = Runner(CommandResult(0, '{"version": 1}'))
    value, error = acquire_baseline(runner, tmp_path, "a" * 40, RegistryFamily.CONTEXT)
    assert value == {"version": 1} and error is None
    assert runner.calls == [("show", "a" * 40 + ":settings/context_overrides.json")]
    _, error = acquire_baseline(runner, tmp_path, "--bad", RegistryFamily.CONTEXT)
    assert error.code is MigrationCode.INVALID_INSTALLED_COMMIT


def test_clean_is_not_required_and_staged_is_never_approved():
    clean = plan_legacy_registry_migration(RegistryFamily.CONTEXT, baseline=context(), local=context(), target=context(), existing_user=None, installed_commit="a" * 40, target_identity="b" * 40, state="clean")
    assert clean.status is MigrationStatus.NOT_REQUIRED and not clean.execution_permitted
    staged = plan_legacy_registry_migration(RegistryFamily.CONTEXT, baseline=context(), local=context(), target=context(), existing_user=None, installed_commit="a" * 40, target_identity="b" * 40, state="staged")
    assert staged.blockers[0].code is MigrationCode.UNSUPPORTED_STATE
    assert summarize_legacy_migration(clean) is LegacyMigrationAssessment.NOT_REQUIRED
    assert summarize_legacy_migration(staged) is LegacyMigrationAssessment.BLOCKED


@pytest.mark.parametrize("result, expected", [
    (CommandResult(1), MigrationCode.BASELINE_UNAVAILABLE),
    (CommandResult(1, timed_out=True), MigrationCode.BASELINE_UNAVAILABLE),
    (CommandResult(0, "not-json"), MigrationCode.BASELINE_MALFORMED),
    (CommandResult(0, "x" * 65536), MigrationCode.BASELINE_UNAVAILABLE),
])
def test_baseline_acquisition_failures_are_blocking(tmp_path, result, expected):
    _, blocker = acquire_baseline(Runner(result), tmp_path, "a" * 40, RegistryFamily.MODALITY)
    assert blocker.code is expected


def test_baseline_acquisition_rejects_unknown_path_identity_and_runner_failure(tmp_path):
    class FailingRunner:
        def run(self, args, *, cwd, timeout):
            raise OSError("not exposed")
    _, blocker = acquire_baseline(FailingRunner(), tmp_path, "a" * 40, RegistryFamily.CONTEXT)
    assert blocker.code is MigrationCode.BASELINE_UNAVAILABLE
    _, blocker = acquire_baseline(Runner(CommandResult(0, "{}")), tmp_path, "a" * 40, "arbitrary")
    assert blocker.code is MigrationCode.BASELINE_UNAVAILABLE


@pytest.mark.parametrize("field", ["families_multimodal", "name_contains_multimodal", "regex_multimodal"])
def test_modality_trigger_matrix_preserves_tombstones_and_target_additions(field):
    base, local, target = modality(), modality(), modality()
    item = base[field][0]
    local[field] = []
    target[field] = [item, "new"]
    plan = plan_legacy_registry_migration(RegistryFamily.MODALITY, baseline=base, local=local, target=target, existing_user=None, installed_commit="a" * 40, target_identity="b" * 40)
    assert plan.status is MigrationStatus.READY  # target addition is non-overlapping
    target[field] = ["replacement"]
    plan = plan_legacy_registry_migration(RegistryFamily.MODALITY, baseline=base, local=local, target=target, existing_user=None, installed_commit="a" * 40, target_identity="b" * 40)
    assert plan.status is MigrationStatus.READY
    assert item in plan.projected_user_document["remove"][field]


def test_existing_user_composition_identical_and_malformed_cases():
    local = context(); local["by_name"]["model"] = 8192
    user = cu(); user["by_name"]["model"] = 8192
    ready = plan_legacy_registry_migration(RegistryFamily.CONTEXT, baseline=context(), local=local, target=context(), existing_user=user, installed_commit="a" * 40, target_identity="b" * 40)
    assert ready.status is MigrationStatus.READY
    assert summarize_legacy_migration(ready) is LegacyMigrationAssessment.POTENTIALLY_MIGRATABLE
    malformed = plan_legacy_registry_migration(RegistryFamily.CONTEXT, baseline=context(), local=local, target=context(), existing_user={"version": 2}, installed_commit="a" * 40, target_identity="b" * 40)
    assert malformed.blockers[0].code is MigrationCode.USER_LAYER_MALFORMED


def test_deterministic_digests_no_write_and_invalid_local_target():
    local = context(); local["by_name"]["new"] = 8192
    before = (context(), local.copy(), context(), cu())
    first = plan_legacy_registry_migration(RegistryFamily.CONTEXT, baseline=before[0], local=before[1], target=before[2], existing_user=before[3], installed_commit="a" * 40, target_identity="b" * 40)
    second = plan_legacy_registry_migration(RegistryFamily.CONTEXT, baseline=before[0], local=before[1], target=before[2], existing_user=before[3], installed_commit="a" * 40, target_identity="b" * 40)
    assert first.projected_digest == second.projected_digest and before[1]["by_name"]["new"] == 8192
    bad = plan_legacy_registry_migration(RegistryFamily.CONTEXT, baseline=context(), local={"version": 1}, target=context(), existing_user=None, installed_commit="a" * 40, target_identity="b" * 40)
    assert bad.blockers[0].code is MigrationCode.LOCAL_MALFORMED
    bad_target = plan_legacy_registry_migration(RegistryFamily.CONTEXT, baseline=context(), local=local, target={"version": 2}, existing_user=None, installed_commit="a" * 40, target_identity="b" * 40)
    assert bad_target.blockers[0].code is MigrationCode.TARGET_MALFORMED


@pytest.mark.parametrize(
    ("local_value", "target_value", "status"),
    [(8192, 1024, MigrationStatus.READY), (8192, 8192, MigrationStatus.READY), (8192, 4096, MigrationStatus.BLOCKED)],
)
def test_context_replacement_target_matrix(local_value, target_value, status):
    local, target = context(), context()
    local["by_name"]["model"] = local_value
    target["by_name"]["model"] = target_value
    plan = plan_legacy_registry_migration(RegistryFamily.CONTEXT, baseline=context(), local=local, target=target, existing_user=None, installed_commit="a" * 40, target_identity="b" * 40)
    assert plan.status is status


@pytest.mark.parametrize("state, code", [("deleted", MigrationCode.TRACKED_DELETED), ("unmerged", MigrationCode.UNSUPPORTED_STATE), ("staged", MigrationCode.UNSUPPORTED_STATE)])
def test_deleted_unmerged_and_staged_registry_states_block(state, code):
    plan = plan_legacy_registry_migration(RegistryFamily.MODALITY, baseline=modality(), local=None if state == "deleted" else modality(), target=modality(), existing_user=None, installed_commit="a" * 40, target_identity="b" * 40, state=state)
    assert plan.status is MigrationStatus.BLOCKED and plan.blockers[0].code is code


def test_modality_override_target_and_existing_user_matrix():
    local, target = modality(), modality()
    local["model_overrides"]["model"] = "text"
    target["model_overrides"]["model"] = "text"
    plan = plan_legacy_registry_migration(RegistryFamily.MODALITY, baseline=modality(), local=local, target=target, existing_user=None, installed_commit="a" * 40, target_identity="b" * 40)
    assert plan.status is MigrationStatus.READY
    target["model_overrides"]["model"] = "multimodal"
    user = mu(); user["remove_model_overrides"] = ["model"]
    plan = plan_legacy_registry_migration(RegistryFamily.MODALITY, baseline=modality(), local=local, target=target, existing_user=user, installed_commit="a" * 40, target_identity="b" * 40)
    assert plan.status is MigrationStatus.BLOCKED


def test_modality_model_override_addition_removal_and_target_conflicts():
    base, local, target = modality(), modality(), modality()
    local["model_overrides"]["new"] = "text"
    target["model_overrides"]["new"] = "multimodal"
    divergent = plan_legacy_registry_migration(
        RegistryFamily.MODALITY, baseline=base, local=local, target=target,
        existing_user=None, installed_commit="a" * 40, target_identity="b" * 40,
    )
    assert divergent.blockers[0].code is MigrationCode.DIVERGENT_CHANGE
    local = modality(); del local["model_overrides"]["model"]
    target = modality(); target["model_overrides"]["model"] = "text"
    divergent = plan_legacy_registry_migration(
        RegistryFamily.MODALITY, baseline=base, local=local, target=target,
        existing_user=None, installed_commit="a" * 40, target_identity="b" * 40,
    )
    assert divergent.blockers[0].code is MigrationCode.DIVERGENT_CHANGE


def test_modality_existing_user_composition_and_malformed_local_are_controlled():
    local = modality(); local["families_multimodal"].append("legacy")
    user = mu(); user["add"] = {"families_multimodal": ["user"]}
    ready = plan_legacy_registry_migration(
        RegistryFamily.MODALITY, baseline=modality(), local=local, target=modality(),
        existing_user=user, installed_commit="a" * 40, target_identity="b" * 40,
    )
    assert ready.status is MigrationStatus.READY
    assert ready.projected_user_document["add"]["families_multimodal"] == ["user", "legacy"]
    user = mu(); user["remove"] = {"families_multimodal": ["legacy"]}
    blocked = plan_legacy_registry_migration(
        RegistryFamily.MODALITY, baseline=modality(), local=local, target=modality(),
        existing_user=user, installed_commit="a" * 40, target_identity="b" * 40,
    )
    assert blocked.blockers[0].code is MigrationCode.USER_CONFLICT
    invalid = modality(); invalid["regex_multimodal"] = ["["]
    malformed = plan_legacy_registry_migration(
        RegistryFamily.MODALITY, baseline=modality(), local=invalid, target=modality(),
        existing_user=None, installed_commit="a" * 40, target_identity="b" * 40,
    )
    assert malformed.blockers[0].code is MigrationCode.LOCAL_MALFORMED


def test_invalid_installed_identity_blocks_planning():
    plan = plan_legacy_registry_migration(RegistryFamily.CONTEXT, baseline=context(), local=context(), target=context(), existing_user=None, installed_commit="BAD", target_identity="b" * 40)
    assert plan.blockers[0].code is MigrationCode.INVALID_INSTALLED_COMMIT


def test_invalid_target_identity_and_malformed_baseline_block_planning():
    plan = plan_legacy_registry_migration(
        RegistryFamily.CONTEXT, baseline=context(), local=context(),
        target=context(), existing_user=None, installed_commit="a" * 40,
        target_identity="not-a-commit",
    )
    assert plan.blockers[0].code is MigrationCode.INVALID_TARGET_IDENTITY
    malformed = plan_legacy_registry_migration(
        RegistryFamily.CONTEXT, baseline={"version": 1}, local=context(),
        target=context(), existing_user=None, installed_commit="a" * 40,
        target_identity="b" * 40,
    )
    assert malformed.blockers[0].code is MigrationCode.BASELINE_MALFORMED
    future = plan_legacy_registry_migration(
        RegistryFamily.CONTEXT, baseline={"version": 2}, local=context(),
        target=context(), existing_user=None, installed_commit="a" * 40,
        target_identity="b" * 40,
    )
    assert future.blockers[0].code is MigrationCode.BASELINE_MALFORMED


@pytest.mark.parametrize(
    ("operation", "target_value", "expected"),
    [
        ("add", 8192, MigrationStatus.READY),
        ("add", 16384, MigrationStatus.BLOCKED),
        ("remove", None, MigrationStatus.READY),
        ("remove", 8192, MigrationStatus.BLOCKED),
    ],
)
def test_context_addition_and_removal_target_matrix(operation, target_value, expected):
    base, local, target = context(), context(), context()
    if operation == "add":
        local["by_name"]["new"] = 8192
        if target_value is not None:
            target["by_name"]["new"] = target_value
    else:
        del local["by_name"]["model"]
        if target_value is None:
            del target["by_name"]["model"]
        else:
            target["by_name"]["model"] = target_value
    plan = plan_legacy_registry_migration(
        RegistryFamily.CONTEXT, baseline=base, local=local, target=target,
        existing_user=None, installed_commit="a" * 40, target_identity="b" * 40,
    )
    assert plan.status is expected


@pytest.mark.parametrize("field", ["by_name", "by_regex", "default_by_family"])
def test_context_each_collection_migrates_tombstones_and_preserves_target_only_changes(field):
    base, local, target = context(), context(), context()
    key = next(iter(base[field]))
    del local[field][key]
    target[field]["target-only" if field != "by_regex" else "target.*"] = 8192
    plan = plan_legacy_registry_migration(
        RegistryFamily.CONTEXT, baseline=base, local=local, target=target,
        existing_user=None, installed_commit="a" * 40, target_identity="b" * 40,
    )
    assert plan.status is MigrationStatus.READY
    assert key in plan.projected_user_document["remove_" + field]


def test_context_regex_add_replace_remove_are_validated_as_logical_patterns():
    base, local = context(), context()
    local["by_regex"]["new.*"] = 8192
    local["by_regex"]["a.*"] = 4096
    plan = plan_legacy_registry_migration(
        RegistryFamily.CONTEXT, baseline=base, local=local, target=base,
        existing_user=None, installed_commit="a" * 40, target_identity="b" * 40,
    )
    assert plan.status is MigrationStatus.READY
    assert plan.projected_user_document["by_regex"] == {"a.*": 4096, "new.*": 8192}
    local["by_regex"]["["] = 2048
    invalid = plan_legacy_registry_migration(
        RegistryFamily.CONTEXT, baseline=base, local=local, target=base,
        existing_user=None, installed_commit="a" * 40, target_identity="b" * 40,
    )
    assert invalid.blockers[0].code is MigrationCode.LOCAL_MALFORMED


@pytest.mark.parametrize("field", ["families_multimodal", "name_contains_multimodal", "regex_multimodal"])
def test_modality_additions_keep_local_order_and_exactly_deduplicate(field):
    base, local = modality(), modality()
    local[field].extend(["two", "one", "two"])
    plan = plan_legacy_registry_migration(
        RegistryFamily.MODALITY, baseline=base, local=local, target=base,
        existing_user=None, installed_commit="a" * 40, target_identity="b" * 40,
    )
    assert plan.status is MigrationStatus.READY
    assert plan.projected_user_document["add"][field] == ["two", "one"]


def test_modality_order_and_normalized_override_collision_block():
    base = modality(); base["families_multimodal"] = ["a", "x"]
    local = modality(); local["families_multimodal"] = ["x", "a"]
    reorder = plan_legacy_registry_migration(
        RegistryFamily.MODALITY, baseline=base, local=local, target=base,
        existing_user=None, installed_commit="a" * 40, target_identity="b" * 40,
    )
    assert reorder.blockers[0].code is MigrationCode.UNREPRESENTABLE_ORDER
    local = modality(); local["model_overrides"] = {"Model": "text", "model": "multimodal"}
    collision = plan_legacy_registry_migration(
        RegistryFamily.MODALITY, baseline=base, local=local, target=base,
        existing_user=None, installed_commit="a" * 40, target_identity="b" * 40,
    )
    assert collision.blockers[0].code is MigrationCode.LOCAL_MALFORMED


def test_existing_user_non_overlapping_operations_are_retained_and_contradictions_block():
    local = context(); local["by_name"]["legacy"] = 8192
    user = cu(); user["by_name"]["user"] = 16384
    ready = plan_legacy_registry_migration(
        RegistryFamily.CONTEXT, baseline=context(), local=local, target=context(),
        existing_user=user, installed_commit="a" * 40, target_identity="b" * 40,
    )
    assert ready.status is MigrationStatus.READY
    assert ready.projected_user_document["by_name"] == {"user": 16384, "legacy": 8192}
    user = cu(); user["remove_by_name"] = ["legacy"]
    blocked = plan_legacy_registry_migration(
        RegistryFamily.CONTEXT, baseline=context(), local=local, target=context(),
        existing_user=user, installed_commit="a" * 40, target_identity="b" * 40,
    )
    assert blocked.blockers[0].code is MigrationCode.USER_CONFLICT


def test_semantic_verification_keeps_target_unrelated_changes_and_local_intent():
    base, local, target = context(), context(), context()
    local["by_name"]["model"] = 8192
    target["by_name"]["target-only"] = 16384
    plan = plan_legacy_registry_migration(
        RegistryFamily.CONTEXT, baseline=base, local=local, target=target,
        existing_user=None, installed_commit="a" * 40, target_identity="b" * 40,
    )
    assert plan.status is MigrationStatus.READY
    parsed, error = registry_layers._parse_context_user(plan.projected_user_document, "test")
    assert error is None
    effective = registry_layers._merge_context(
        registry_layers._parse_context_shipped(target, "test")[0], parsed,
    )
    assert effective.by_name == {"model": 8192, "target-only": 16384}


def test_git_backed_planner_uses_only_fixed_show_and_never_substitutes_worktree(tmp_path):
    baseline = context()
    runner = Runner(CommandResult(0, __import__("json").dumps(baseline)))
    local = context(); local["by_name"]["legacy"] = 8192
    plan = plan_legacy_registry_migration_from_git(
        RegistryFamily.CONTEXT, runner=runner, root=tmp_path, local=local,
        target=context(), existing_user=None, installed_commit="a" * 40,
        target_identity="b" * 40,
    )
    assert plan.status is MigrationStatus.READY
    assert runner.calls == [("show", "a" * 40 + ":settings/context_overrides.json")]


def test_fixture_and_existing_user_bytes_are_untouched(tmp_path):
    path = tmp_path / "context_overrides.user.json"
    assert not path.exists()
    base, local, target, user = context(), context(), context(), cu()
    user_bytes = json.dumps(user, indent=2).encode("utf-8")
    path.write_bytes(user_bytes)
    before = tuple(json.dumps(value, sort_keys=False) for value in (base, local, target, user))
    plan_legacy_registry_migration(
        RegistryFamily.CONTEXT, baseline=base, local=local, target=target,
        existing_user=user, installed_commit="a" * 40, target_identity="b" * 40,
    )
    after = tuple(json.dumps(value, sort_keys=False) for value in (base, local, target, user))
    assert before == after and path.read_bytes() == user_bytes


def test_byte_different_but_semantically_equal_edit_is_reported_without_invented_operation():
    base = context()
    # Dict order has no context lookup meaning; JSON formatting/order differs but data do not.
    local = {"default_by_family": {"family": 4096}, "by_regex": {"a.*": 2048}, "by_name": {"model": 1024}, "version": 1}
    assert json.dumps(base, indent=2).encode("utf-8") != json.dumps(local, separators=(",", ":")).encode("utf-8")
    plan = plan_legacy_registry_migration(
        RegistryFamily.CONTEXT, baseline=base, local=local, target=base,
        existing_user=None, installed_commit="a" * 40, target_identity="b" * 40,
    )
    assert plan.status is MigrationStatus.NOT_REQUIRED
    assert plan.semantically_unchanged_legacy_edit
    assert not plan.execution_permitted
