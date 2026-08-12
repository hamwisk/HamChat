"""Pure three-way planner for legacy edits to tracked knowledge registries."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Any

from .infra.llm import registry_layers as layers
from .update_assessment import CommandResult, GitRunner


class RegistryFamily(str, Enum):
    CONTEXT = "context"
    MODALITY = "modality"


class MigrationStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    READY = "ready"
    BLOCKED = "blocked"


class MigrationCode(str, Enum):
    INVALID_INSTALLED_COMMIT = "invalid_installed_commit"
    INVALID_TARGET_IDENTITY = "invalid_target_identity"
    BASELINE_UNAVAILABLE = "baseline_unavailable"
    BASELINE_MALFORMED = "baseline_malformed"
    LOCAL_MALFORMED = "local_malformed"
    TARGET_MALFORMED = "target_malformed"
    USER_LAYER_MALFORMED = "existing_user_layer_malformed"
    DIVERGENT_CHANGE = "divergent_local_target_change"
    USER_CONFLICT = "divergent_legacy_user_operation"
    UNREPRESENTABLE_ORDER = "unrepresentable_ordering_change"
    RELEASE_OWNED_CHANGE = "unrepresentable_release_owned_field_change"
    TRACKED_DELETED = "tracked_registry_deleted"
    UNSUPPORTED_STATE = "unsupported_staged_or_unmerged_state"
    SEMANTIC_VERIFICATION_FAILED = "semantic_verification_failed"


@dataclass(frozen=True)
class MigrationBlocker:
    code: MigrationCode
    field: str | None = None
    key: str | None = None


@dataclass(frozen=True)
class LegacyMigrationPlan:
    family: RegistryFamily
    status: MigrationStatus
    installed_commit: str
    target_identity: str
    input_digests: tuple[tuple[str, str], ...]
    projected_user_document: dict[str, Any] | None
    projected_digest: str | None
    blockers: tuple[MigrationBlocker, ...]
    preservation_paths: tuple[str, ...]
    backup_required: bool
    execution_permitted: bool = False
    semantically_unchanged_legacy_edit: bool = False


_PATHS = {RegistryFamily.CONTEXT: "settings/context_overrides.json", RegistryFamily.MODALITY: "settings/modality_triggers.json"}


def _is_commit_identity(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(char in "0123456789abcdef" for char in value)
    )


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def acquire_baseline(runner: GitRunner, root: Path, installed_commit: str, family: RegistryFamily, timeout: float = 5.0) -> tuple[object | None, MigrationBlocker | None]:
    if not _is_commit_identity(installed_commit):
        return None, MigrationBlocker(MigrationCode.INVALID_INSTALLED_COMMIT)
    if not isinstance(family, RegistryFamily):
        return None, MigrationBlocker(MigrationCode.BASELINE_UNAVAILABLE)
    path = _PATHS[family]
    try:
        result: CommandResult = runner.run(("show", f"{installed_commit}:{path}"), cwd=root, timeout=timeout)
    except (OSError, ValueError):
        return None, MigrationBlocker(MigrationCode.BASELINE_UNAVAILABLE)
    if result.timed_out or result.returncode != 0 or len(result.stdout.encode("utf-8")) >= 65536:
        return None, MigrationBlocker(MigrationCode.BASELINE_UNAVAILABLE)
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError:
        return None, MigrationBlocker(MigrationCode.BASELINE_MALFORMED)


def _parse(family: RegistryFamily, raw: object, source: str):
    return layers._parse_context_shipped(raw, source) if family is RegistryFamily.CONTEXT else layers._parse_modality_shipped(raw, source)


def _user_parse(family: RegistryFamily, raw: object, source: str):
    return layers._parse_context_user(raw, source) if family is RegistryFamily.CONTEXT else layers._parse_modality_user(raw, source)


def _empty_user(family: RegistryFamily) -> dict[str, Any]:
    if family is RegistryFamily.CONTEXT:
        return {"version": 1, "by_name": {}, "by_regex": {}, "default_by_family": {}, "remove_by_name": [], "remove_by_regex": [], "remove_default_by_family": []}
    return {"version": 1, "add": {}, "remove": {}, "model_overrides": {}, "remove_model_overrides": []}


def _block(family, installed, target, code, field=None, key=None, *values):
    return LegacyMigrationPlan(family, MigrationStatus.BLOCKED, installed, target, tuple((str(i), _digest(v)) for i, v in enumerate(values)), None, None, (MigrationBlocker(code, field, key),), (), True)


def plan_legacy_registry_migration(family: RegistryFamily, *, baseline: object, local: object | None, target: object, existing_user: object | None, installed_commit: str, target_identity: str, state: str = "modified") -> LegacyMigrationPlan:
    """Compare validated B/L/T/U data only; this function never writes."""
    if not _is_commit_identity(installed_commit):
        return _block(family, installed_commit, target_identity, MigrationCode.INVALID_INSTALLED_COMMIT, None, None, baseline, target)
    if not _is_commit_identity(target_identity):
        return _block(family, installed_commit, target_identity, MigrationCode.INVALID_TARGET_IDENTITY, None, None, baseline, target)
    if state in {"deleted"} or local is None:
        return _block(family, installed_commit, target_identity, MigrationCode.TRACKED_DELETED, None, None, baseline, target)
    if state not in {"modified", "clean"}:
        return _block(family, installed_commit, target_identity, MigrationCode.UNSUPPORTED_STATE, None, None, baseline, local, target)
    b, error = _parse(family, baseline, "baseline")
    if error: return _block(family, installed_commit, target_identity, MigrationCode.BASELINE_MALFORMED, error.field, None, baseline)
    l, error = _parse(family, local, "local")
    if error: return _block(family, installed_commit, target_identity, MigrationCode.LOCAL_MALFORMED, error.field, None, local)
    t, error = _parse(family, target, "target")
    if error: return _block(family, installed_commit, target_identity, MigrationCode.TARGET_MALFORMED, error.field, None, target)
    user_doc = _empty_user(family) if existing_user is None else existing_user
    u, error = _user_parse(family, user_doc, "user")
    if error: return _block(family, installed_commit, target_identity, MigrationCode.USER_LAYER_MALFORMED, error.field, None, user_doc)
    if state == "clean":
        return LegacyMigrationPlan(family, MigrationStatus.NOT_REQUIRED, installed_commit, target_identity, (("baseline", _digest(baseline)), ("local", _digest(local)), ("target", _digest(target)), ("user", _digest(user_doc))), None, None, (), (), False)
    if family is RegistryFamily.CONTEXT:
        # Regex order is behaviorally significant; user schema has no reorder operation.
        if list(b.by_regex) != list(l.by_regex) and set(b.by_regex) == set(l.by_regex) and b.by_regex == l.by_regex:
            return _block(family, installed_commit, target_identity, MigrationCode.UNREPRESENTABLE_ORDER, "by_regex", None, baseline, local)
        projected = _plan_context(b, l, t, user_doc)
    else:
        if l.defaults_to != b.defaults_to:
            return _block(family, installed_commit, target_identity, MigrationCode.RELEASE_OWNED_CHANGE, "defaults_to", None, baseline, local)
        for field in layers._MODALITY_LISTS:
            if list(b.trigger_lists[field]) != list(l.trigger_lists[field]) and set(b.trigger_lists[field]) == set(l.trigger_lists[field]):
                return _block(family, installed_commit, target_identity, MigrationCode.UNREPRESENTABLE_ORDER, field, None, baseline, local)
        projected = _plan_modality(b, l, t, user_doc)
    if isinstance(projected, MigrationBlocker):
        return _block(family, installed_commit, target_identity, projected.code, projected.field, projected.key, baseline, local, target, user_doc)
    # Validate through the production user-layer parser before any semantic merge.
    parsed, error = _user_parse(family, projected, "projected")
    if error:
        return _block(family, installed_commit, target_identity, MigrationCode.USER_CONFLICT, error.field, None, projected)
    if not _verify_effective_result(family, b, l, t, u, parsed):
        return _block(family, installed_commit, target_identity, MigrationCode.SEMANTIC_VERIFICATION_FAILED, None, None, baseline, local, target, user_doc, projected)
    unchanged = projected == _empty_user(family)
    return LegacyMigrationPlan(family, MigrationStatus.NOT_REQUIRED if unchanged else MigrationStatus.READY, installed_commit, target_identity, (("baseline", _digest(baseline)), ("local", _digest(local)), ("target", _digest(target)), ("user", _digest(user_doc))), projected if not unchanged else None, _digest(projected) if not unchanged else None, (), (_PATHS[family], _PATHS[family].replace(".json", ".user.json")), not unchanged, semantically_unchanged_legacy_edit=(b == l))


def _plan_context(b, l, t, udoc):
    projected = json.loads(json.dumps(udoc))
    for field, removal in (("by_name", "remove_by_name"), ("by_regex", "remove_by_regex"), ("default_by_family", "remove_default_by_family")):
        existing_add, existing_remove = projected[field], set(projected[removal])
        for key in sorted(set(getattr(b, field)) | set(getattr(l, field))):
            bv, lv, tv = getattr(b, field).get(key), getattr(l, field).get(key), getattr(t, field).get(key)
            if bv == lv: continue
            if tv != bv and tv != lv: return MigrationBlocker(MigrationCode.DIVERGENT_CHANGE, field, key)
            if lv is None:
                if key in existing_add: return MigrationBlocker(MigrationCode.USER_CONFLICT, field, key)
                if key not in existing_remove: projected[removal].append(key)
            else:
                if key in existing_remove or (key in existing_add and existing_add[key] != lv): return MigrationBlocker(MigrationCode.USER_CONFLICT, field, key)
                projected[field][key] = lv
    return projected


def _plan_modality(b, l, t, udoc):
    projected = json.loads(json.dumps(udoc))
    for field in layers._MODALITY_LISTS:
        add, remove = projected["add"].setdefault(field, []), projected["remove"].setdefault(field, [])
        for item in sorted(set(b.trigger_lists[field]) | set(l.trigger_lists[field])):
            in_baseline = item in b.trigger_lists[field]
            in_local = item in l.trigger_lists[field]
            in_target = item in t.trigger_lists[field]
            if in_local != in_baseline and in_target != in_baseline and in_target != in_local:
                return MigrationBlocker(MigrationCode.DIVERGENT_CHANGE, field, item)
        for item in l.trigger_lists[field]:
            if item not in b.trigger_lists[field] and item not in add:
                if item in remove: return MigrationBlocker(MigrationCode.USER_CONFLICT, field, item)
                add.append(item)
        for item in b.trigger_lists[field]:
            if item not in l.trigger_lists[field] and item not in remove:
                if item in add: return MigrationBlocker(MigrationCode.USER_CONFLICT, field, item)
                remove.append(item)
    for key in sorted(set(b.model_overrides) | set(l.model_overrides)):
        bv, lv, tv = b.model_overrides.get(key), l.model_overrides.get(key), t.model_overrides.get(key)
        if bv == lv: continue
        if tv != bv and tv != lv: return MigrationBlocker(MigrationCode.DIVERGENT_CHANGE, "model_overrides", key)
        if lv is None:
            if key in projected["model_overrides"]: return MigrationBlocker(MigrationCode.USER_CONFLICT, "model_overrides", key)
            if key not in projected["remove_model_overrides"]: projected["remove_model_overrides"].append(key)
        else:
            if key in projected["remove_model_overrides"] or (key in projected["model_overrides"] and projected["model_overrides"][key] != lv): return MigrationBlocker(MigrationCode.USER_CONFLICT, "model_overrides", key)
            projected["model_overrides"][key] = lv
    return projected


def _verify_effective_result(family, baseline, local, target, existing_user, projected_user):
    """Verify B→L intent survives T+U' without accepting a weakened schema."""
    if family is RegistryFamily.CONTEXT:
        effective = layers._merge_context(target, projected_user)
        for field in ("by_name", "by_regex", "default_by_family"):
            for key in set(getattr(baseline, field)) | set(getattr(local, field)):
                baseline_value = getattr(baseline, field).get(key)
                local_value = getattr(local, field).get(key)
                if baseline_value != local_value and getattr(effective, field).get(key) != local_value:
                    return False
        return True
    effective = layers._merge_modality(target, projected_user)
    for field in layers._MODALITY_LISTS:
        for item in set(baseline.trigger_lists[field]) | set(local.trigger_lists[field]):
            if ((item in baseline.trigger_lists[field]) != (item in local.trigger_lists[field])
                    and (item in effective.trigger_lists[field]) != (item in local.trigger_lists[field])):
                return False
    for key in set(baseline.model_overrides) | set(local.model_overrides):
        baseline_value = baseline.model_overrides.get(key)
        local_value = local.model_overrides.get(key)
        if baseline_value != local_value and effective.model_overrides.get(key) != local_value:
            return False
    return effective.defaults_to == target.defaults_to


def plan_legacy_registry_migration_from_git(
    family: RegistryFamily, *, runner: GitRunner, root: Path, local: object | None,
    target: object, existing_user: object | None, installed_commit: str,
    target_identity: str, state: str = "modified", timeout: float = 5.0,
) -> LegacyMigrationPlan:
    """Acquire only the fixed baseline blob, then delegate to the pure planner."""
    baseline, blocker = acquire_baseline(runner, root, installed_commit, family, timeout)
    if blocker:
        return _block(family, installed_commit, target_identity, blocker.code, blocker.field, blocker.key, local, target, existing_user)
    return plan_legacy_registry_migration(
        family, baseline=baseline, local=local, target=target,
        existing_user=existing_user, installed_commit=installed_commit,
        target_identity=target_identity, state=state,
    )
