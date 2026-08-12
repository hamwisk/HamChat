"""Read-only validated shipped/user knowledge registries.

User files are optional, never repaired, and never partially applied.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
import re
from typing import Any, Mapping


CONTEXT_FILE = "context_overrides.json"
CONTEXT_USER_FILE = "context_overrides.user.json"
MODALITY_FILE = "modality_triggers.json"
MODALITY_USER_FILE = "modality_triggers.user.json"
_CONTEXT_FIELDS = frozenset({"version", "by_name", "by_regex", "default_by_family"})
_MODALITY_LISTS = ("families_multimodal", "name_contains_multimodal", "regex_multimodal")
_MODALITY_FIELDS = frozenset({"version", "defaults_to", *_MODALITY_LISTS, "model_overrides"})
_MODALITIES = frozenset({"text", "multimodal"})


class RegistryDiagnosticCode(str, Enum):
    MISSING_FILE = "missing_file"
    MALFORMED_JSON = "malformed_json"
    WRONG_TOP_LEVEL_TYPE = "wrong_top_level_type"
    UNSUPPORTED_VERSION = "unsupported_version"
    MISSING_FIELD = "missing_field"
    UNEXPECTED_FIELD = "unexpected_field"
    WRONG_FIELD_TYPE = "wrong_field_type"
    INVALID_CONTEXT_VALUE = "invalid_context_value"
    INVALID_REGEX = "invalid_regex"
    INVALID_MODALITY_VALUE = "invalid_modality_value"
    INVALID_MODEL_KEY = "invalid_normalized_model_key"
    NORMALIZATION_COLLISION = "normalization_collision"
    CONTRADICTORY_OPERATION = "contradictory_operation"


@dataclass(frozen=True)
class RegistryDiagnostic:
    source: str
    code: RegistryDiagnosticCode
    field: str | None = None


@dataclass(frozen=True)
class ContextRegistry:
    by_name: dict[str, int]
    by_regex: dict[str, int]
    default_by_family: dict[str, int]


@dataclass(frozen=True)
class ModalityRegistry:
    defaults_to: str
    trigger_lists: dict[str, tuple[str, ...]]
    model_overrides: dict[str, str]


@dataclass(frozen=True)
class RegistryLoadResult:
    context: ContextRegistry
    modality: ModalityRegistry
    diagnostics: tuple[RegistryDiagnostic, ...]


def normalize_model_key(value: object) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip() or any(char.isspace() for char in value):
        return None
    return value.lower()


def _context_value(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 128 <= value <= 10_000_000 else None


def _read(path: Path) -> tuple[object | None, RegistryDiagnostic | None]:
    if not path.exists():
        return None, RegistryDiagnostic(path.name, RegistryDiagnosticCode.MISSING_FILE)
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, RegistryDiagnostic(path.name, RegistryDiagnosticCode.MALFORMED_JSON)


def _object(value: object, source: str, fields: frozenset[str]) -> RegistryDiagnostic | None:
    if not isinstance(value, dict):
        return RegistryDiagnostic(source, RegistryDiagnosticCode.WRONG_TOP_LEVEL_TYPE)
    if value.get("version") != 1 or isinstance(value.get("version"), bool):
        return RegistryDiagnostic(source, RegistryDiagnosticCode.UNSUPPORTED_VERSION, "version")
    missing = fields - set(value)
    if missing:
        return RegistryDiagnostic(source, RegistryDiagnosticCode.MISSING_FIELD, sorted(missing)[0])
    extra = set(value) - fields
    if extra:
        return RegistryDiagnostic(source, RegistryDiagnosticCode.UNEXPECTED_FIELD, sorted(extra)[0])
    return None


def _context_mapping(value: object, source: str, field: str, *, regex: bool = False, model: bool = False) -> tuple[dict[str, int] | None, RegistryDiagnostic | None]:
    if not isinstance(value, dict):
        return None, RegistryDiagnostic(source, RegistryDiagnosticCode.WRONG_FIELD_TYPE, field)
    result: dict[str, int] = {}
    for raw_key, raw_value in value.items():
        key = normalize_model_key(raw_key) if model else raw_key.lower() if isinstance(raw_key, str) and raw_key and raw_key == raw_key.strip() else None
        if key is None:
            return None, RegistryDiagnostic(source, RegistryDiagnosticCode.INVALID_MODEL_KEY, field)
        if key in result:
            return None, RegistryDiagnostic(source, RegistryDiagnosticCode.NORMALIZATION_COLLISION, field)
        if regex:
            try:
                re.compile(raw_key, re.IGNORECASE)
            except (TypeError, re.error):
                return None, RegistryDiagnostic(source, RegistryDiagnosticCode.INVALID_REGEX, field)
            key = raw_key
        context = _context_value(raw_value)
        if context is None:
            return None, RegistryDiagnostic(source, RegistryDiagnosticCode.INVALID_CONTEXT_VALUE, field)
        result[key] = context
    return result, None


def _parse_context_shipped(value: object, source: str) -> tuple[ContextRegistry | None, RegistryDiagnostic | None]:
    error = _object(value, source, _CONTEXT_FIELDS)
    if error:
        return None, error
    by_name, error = _context_mapping(value["by_name"], source, "by_name", model=True)
    if error: return None, error
    by_regex, error = _context_mapping(value["by_regex"], source, "by_regex", regex=True)
    if error: return None, error
    families, error = _context_mapping(value["default_by_family"], source, "default_by_family")
    if error: return None, error
    return ContextRegistry(by_name, by_regex, families), None


def _parse_context_user(value: object, source: str) -> tuple[tuple[ContextRegistry, dict[str, frozenset[str]]] | None, RegistryDiagnostic | None]:
    fields = _CONTEXT_FIELDS | {"remove_by_name", "remove_by_regex", "remove_default_by_family"}
    error = _object(value, source, fields)
    if error: return None, error
    registry, error = _parse_context_shipped({key: value[key] for key in _CONTEXT_FIELDS}, source)
    if error: return None, error
    removals: dict[str, frozenset[str]] = {}
    for field, target, regex, model in (("remove_by_name", "by_name", False, True), ("remove_by_regex", "by_regex", True, False), ("remove_default_by_family", "default_by_family", False, False)):
        raw = value[field]
        if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
            return None, RegistryDiagnostic(source, RegistryDiagnosticCode.WRONG_FIELD_TYPE, field)
        normalized = frozenset(normalize_model_key(item) if model else item for item in raw)
        if None in normalized or len(normalized) != len(raw):
            return None, RegistryDiagnostic(source, RegistryDiagnosticCode.NORMALIZATION_COLLISION, field)
        if regex:
            for item in normalized:
                try: re.compile(item, re.IGNORECASE)
                except re.error: return None, RegistryDiagnostic(source, RegistryDiagnosticCode.INVALID_REGEX, field)
        if normalized & set(getattr(registry, target)):
            return None, RegistryDiagnostic(source, RegistryDiagnosticCode.CONTRADICTORY_OPERATION, field)
        removals[target] = normalized
    return (registry, removals), None


def _parse_modality_shipped(value: object, source: str) -> tuple[ModalityRegistry | None, RegistryDiagnostic | None]:
    error = _object(value, source, _MODALITY_FIELDS)
    if error: return None, error
    if value["defaults_to"] not in _MODALITIES:
        return None, RegistryDiagnostic(source, RegistryDiagnosticCode.INVALID_MODALITY_VALUE, "defaults_to")
    lists: dict[str, tuple[str, ...]] = {}
    for field in _MODALITY_LISTS:
        raw = value[field]
        if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
            return None, RegistryDiagnostic(source, RegistryDiagnosticCode.WRONG_FIELD_TYPE, field)
        if field == "regex_multimodal":
            try:
                for item in raw: re.compile(item, re.IGNORECASE)
            except re.error:
                return None, RegistryDiagnostic(source, RegistryDiagnosticCode.INVALID_REGEX, field)
        lists[field] = tuple(raw)
    raw_overrides = value["model_overrides"]
    if not isinstance(raw_overrides, dict):
        return None, RegistryDiagnostic(source, RegistryDiagnosticCode.WRONG_FIELD_TYPE, "model_overrides")
    overrides: dict[str, str] = {}
    for raw_key, modality in raw_overrides.items():
        key = normalize_model_key(raw_key)
        if key is None: return None, RegistryDiagnostic(source, RegistryDiagnosticCode.INVALID_MODEL_KEY, "model_overrides")
        if key in overrides: return None, RegistryDiagnostic(source, RegistryDiagnosticCode.NORMALIZATION_COLLISION, "model_overrides")
        if modality not in _MODALITIES: return None, RegistryDiagnostic(source, RegistryDiagnosticCode.INVALID_MODALITY_VALUE, "model_overrides")
        overrides[key] = modality
    return ModalityRegistry(value["defaults_to"], lists, overrides), None


def _parse_modality_user(value: object, source: str) -> tuple[tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]], dict[str, str], frozenset[str]] | None, RegistryDiagnostic | None]:
    fields = frozenset({"version", "add", "remove", "model_overrides", "remove_model_overrides"})
    error = _object(value, source, fields)
    if error: return None, error
    parsed: list[dict[str, tuple[str, ...]]] = []
    for block_name in ("add", "remove"):
        block = value[block_name]
        if not isinstance(block, dict) or set(block) - set(_MODALITY_LISTS):
            return None, RegistryDiagnostic(source, RegistryDiagnosticCode.UNEXPECTED_FIELD, block_name)
        data = {}
        for field in _MODALITY_LISTS:
            items = block.get(field, [])
            if not isinstance(items, list) or not all(isinstance(item, str) and item for item in items):
                return None, RegistryDiagnostic(source, RegistryDiagnosticCode.WRONG_FIELD_TYPE, field)
            if field == "regex_multimodal":
                try:
                    for item in items: re.compile(item, re.IGNORECASE)
                except re.error: return None, RegistryDiagnostic(source, RegistryDiagnosticCode.INVALID_REGEX, field)
            data[field] = tuple(items)
        parsed.append(data)
    if any(set(parsed[0][field]) & set(parsed[1][field]) for field in _MODALITY_LISTS):
        return None, RegistryDiagnostic(source, RegistryDiagnosticCode.CONTRADICTORY_OPERATION)
    overrides, error = _parse_modality_shipped({"version": 1, "defaults_to": "text", **{field: [] for field in _MODALITY_LISTS}, "model_overrides": value["model_overrides"]}, source)
    if error: return None, error
    removals = value["remove_model_overrides"]
    if not isinstance(removals, list): return None, RegistryDiagnostic(source, RegistryDiagnosticCode.WRONG_FIELD_TYPE, "remove_model_overrides")
    removed = frozenset(normalize_model_key(item) for item in removals)
    if None in removed or len(removed) != len(removals): return None, RegistryDiagnostic(source, RegistryDiagnosticCode.NORMALIZATION_COLLISION, "remove_model_overrides")
    if removed & set(overrides.model_overrides): return None, RegistryDiagnostic(source, RegistryDiagnosticCode.CONTRADICTORY_OPERATION, "remove_model_overrides")
    return (parsed[0], parsed[1], overrides.model_overrides, removed), None


def _merge_context(shipped: ContextRegistry, user: tuple[ContextRegistry, dict[str, frozenset[str]]] | None) -> ContextRegistry:
    if user is None: return shipped
    additions, removals = user
    values = {}
    for field in ("by_name", "by_regex", "default_by_family"):
        result = dict(getattr(shipped, field))
        for key in removals[field]: result.pop(key, None)
        result.update(getattr(additions, field))
        values[field] = dict(sorted(result.items()))
    return ContextRegistry(**values)


def _merge_modality(shipped: ModalityRegistry, user: tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]], dict[str, str], frozenset[str]] | None) -> ModalityRegistry:
    if user is None: return shipped
    additions, removals, overrides, removed_overrides = user
    lists = {}
    for field in _MODALITY_LISTS:
        values = [item for item in shipped.trigger_lists[field] if item not in removals[field]] + list(additions[field])
        lists[field] = tuple(dict.fromkeys(values))
    merged_overrides = {key: value for key, value in shipped.model_overrides.items() if key not in removed_overrides}
    merged_overrides.update(overrides)
    return ModalityRegistry(shipped.defaults_to, lists, dict(sorted(merged_overrides.items())))


def load_effective_registries(settings_dir: Path) -> RegistryLoadResult:
    """Load shipped files plus optional user layers without any filesystem mutation."""
    diagnostics: list[RegistryDiagnostic] = []
    raw, error = _read(settings_dir / CONTEXT_FILE)
    shipped_context, error = _parse_context_shipped(raw, CONTEXT_FILE) if error is None else (None, error)
    if error and error.code is not RegistryDiagnosticCode.MISSING_FILE: diagnostics.append(error)
    raw_user, user_error = _read(settings_dir / CONTEXT_USER_FILE)
    user_context = None
    if user_error is None: user_context, user_error = _parse_context_user(raw_user, CONTEXT_USER_FILE)
    if user_error and user_error.code is not RegistryDiagnosticCode.MISSING_FILE: diagnostics.append(user_error)
    raw, error = _read(settings_dir / MODALITY_FILE)
    shipped_modality, error = _parse_modality_shipped(raw, MODALITY_FILE) if error is None else (None, error)
    if error and error.code is not RegistryDiagnosticCode.MISSING_FILE: diagnostics.append(error)
    raw_user, user_error = _read(settings_dir / MODALITY_USER_FILE)
    user_modality = None
    if user_error is None: user_modality, user_error = _parse_modality_user(raw_user, MODALITY_USER_FILE)
    if user_error and user_error.code is not RegistryDiagnosticCode.MISSING_FILE: diagnostics.append(user_error)
    empty_context = ContextRegistry({}, {}, {})
    empty_modality = ModalityRegistry("text", {field: () for field in _MODALITY_LISTS}, {})
    return RegistryLoadResult(_merge_context(shipped_context or empty_context, user_context), _merge_modality(shipped_modality or empty_modality, user_modality), tuple(diagnostics))
