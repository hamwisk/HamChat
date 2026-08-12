# hamchat/updates.py
"""Pure manifest validation, preference handling, and update eligibility."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .settings import save_settings


_SEMVER = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_FIELDS = frozenset(
    ("schema_version", "version", "git_ref", "release_notes", "minimum_updater_version")
)
_SCHEMA_VERSION = 1


class UpdateMode(str, Enum):
    AUTOMATIC = "automatic"
    PROMPT = "prompt"
    OFF = "off"


class DecisionReason(str, Enum):
    UPDATE_AVAILABLE = "update_available"
    UPDATE_MODE_OFF = "update_mode_off"
    REMOTE_NOT_NEWER = "remote_not_newer"
    PATCH_UPDATE_IGNORED = "patch_update_ignored"
    VERSION_SKIPPED = "version_skipped"
    INVALID_INSTALLED_VERSION = "invalid_installed_version"
    INVALID_MANIFEST = "invalid_manifest"
    UNSUPPORTED_MANIFEST_SCHEMA = "unsupported_manifest_schema"
    INVALID_UPDATE_PREFERENCES = "invalid_update_preferences"
    UPDATER_INCOMPATIBLE = "updater_incompatible"


class UpdateValidationError(ValueError):
    pass


@dataclass(frozen=True)
class SemanticVersion:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: object) -> "SemanticVersion":
        if not isinstance(value, str):
            raise UpdateValidationError("version must be a string")
        match = _SEMVER.fullmatch(value)
        if not match:
            raise UpdateValidationError(f"invalid semantic version: {value!r}")
        return cls(
            int(match["major"]), int(match["minor"]), int(match["patch"]),
            tuple(match["pre"].split(".")) if match["pre"] else (),
            tuple(match["build"].split(".")) if match["build"] else (),
        )

    @property
    def is_stable(self) -> bool:
        return not self.prerelease

    def __str__(self) -> str:
        value = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            value += "-" + ".".join(self.prerelease)
        if self.build:
            value += "+" + ".".join(self.build)
        return value

    def compare(self, other: "SemanticVersion") -> int:
        own_core = (self.major, self.minor, self.patch)
        other_core = (other.major, other.minor, other.patch)
        if own_core != other_core:
            return -1 if own_core < other_core else 1
        if not self.prerelease or not other.prerelease:
            if self.prerelease == other.prerelease:
                return 0
            return -1 if self.prerelease else 1
        for left, right in zip(self.prerelease, other.prerelease):
            if left == right:
                continue
            left_number, right_number = left.isdigit(), right.isdigit()
            if left_number and right_number:
                return -1 if int(left) < int(right) else 1
            if left_number != right_number:
                return -1 if left_number else 1
            return -1 if left < right else 1
        if len(self.prerelease) == len(other.prerelease):
            return 0
        return -1 if len(self.prerelease) < len(other.prerelease) else 1


@dataclass(frozen=True)
class UpdatePreferences:
    mode: UpdateMode = UpdateMode.PROMPT
    ignore_patch_updates: bool = False
    skipped_version: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, UpdateMode):
            raise UpdateValidationError("mode must be an UpdateMode")
        if not isinstance(self.ignore_patch_updates, bool):
            raise UpdateValidationError("ignore_patch_updates must be a boolean")
        if self.skipped_version is not None:
            version = SemanticVersion.parse(self.skipped_version)
            if not version.is_stable:
                raise UpdateValidationError("skipped_version must be stable")

    @classmethod
    def from_mapping(cls, value: object) -> "UpdatePreferences":
        expected = {"mode", "ignore_patch_updates", "skipped_version"}
        if not isinstance(value, Mapping) or set(value) != expected:
            raise UpdateValidationError(
                "updates settings must contain exactly mode, "
                "ignore_patch_updates, skipped_version"
            )
        try:
            mode = UpdateMode(value["mode"])
        except (TypeError, ValueError) as exc:
            raise UpdateValidationError("invalid update mode") from exc
        return cls(mode, value["ignore_patch_updates"], value["skipped_version"])

    def as_mapping(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "ignore_patch_updates": self.ignore_patch_updates,
            "skipped_version": self.skipped_version,
        }


def preferences_from_settings(settings: Mapping[str, Any]) -> UpdatePreferences:
    return UpdatePreferences.from_mapping(settings.get("updates", UpdatePreferences().as_mapping()))


def save_update_preferences(path: Path, settings: Mapping[str, Any], preferences: UpdatePreferences) -> dict[str, Any]:
    updated = dict(settings)
    updated["updates"] = preferences.as_mapping()
    save_settings(path, updated)
    return updated


@dataclass(frozen=True)
class ReleaseManifest:
    schema_version: int
    version: SemanticVersion
    git_ref: str
    release_notes: str
    minimum_updater_version: SemanticVersion


@dataclass(frozen=True)
class ManifestValidationResult:
    manifest: ReleaseManifest | None
    reason: DecisionReason | None = None
    detail: str | None = None


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or any(char.isspace() for char in value):
        raise UpdateValidationError(f"{field} must be a non-empty whitespace-free string")
    return value


def _release_notes_path(value: object) -> str:
    text = _text(value, "release_notes")
    path = PurePosixPath(text)
    if ("\\" in text or path.as_posix() != text or path.is_absolute()
            or not path.parts or path.parts[0] != "updates"
            or any(part in {".", ".."} for part in path.parts)):
        raise UpdateValidationError("release_notes must be a safe repository-relative path under updates/")
    return text


def parse_release_manifest(value: object) -> ManifestValidationResult:
    try:
        if not isinstance(value, Mapping):
            raise UpdateValidationError("manifest must be an object")
        missing, extra = _FIELDS - set(value), set(value) - _FIELDS
        if missing or extra:
            raise UpdateValidationError(
                "manifest fields invalid; "
                f"missing={sorted(missing)}, unexpected={sorted(map(repr, extra))}"
            )
        schema = value["schema_version"]
        if isinstance(schema, bool) or not isinstance(schema, int):
            raise UpdateValidationError("schema_version must be an integer")
        if schema != _SCHEMA_VERSION:
            return ManifestValidationResult(None, DecisionReason.UNSUPPORTED_MANIFEST_SCHEMA, f"unsupported schema_version: {schema}")
        release = SemanticVersion.parse(value["version"])
        minimum = SemanticVersion.parse(value["minimum_updater_version"])
        if not release.is_stable:
            raise UpdateValidationError("manifest version must be a stable release")
        return ManifestValidationResult(
            ReleaseManifest(
                schema,
                release,
                _text(value["git_ref"], "git_ref"),
                _release_notes_path(value["release_notes"]),
                minimum,
            )
        )
    except UpdateValidationError as exc:
        return ManifestValidationResult(None, DecisionReason.INVALID_MANIFEST, str(exc))


@dataclass(frozen=True)
class UpdateDecision:
    reason: DecisionReason
    manifest: ReleaseManifest | None = None
    detail: str | None = None

    @property
    def update_available(self) -> bool:
        return self.reason is DecisionReason.UPDATE_AVAILABLE


def decide_update(
    installed_version: object,
    manifest: ReleaseManifest | ManifestValidationResult | Mapping[str, Any] | object,
    preferences: UpdatePreferences | Mapping[str, Any] | object,
    *,
    manual_check: bool = False,
    updater_version: object = None,
) -> UpdateDecision:
    """Decide eligibility without I/O; a manual check only bypasses a skip."""
    try:
        prefs = (
            preferences
            if isinstance(preferences, UpdatePreferences)
            else UpdatePreferences.from_mapping(preferences)
        )
    except UpdateValidationError as exc:
        return UpdateDecision(DecisionReason.INVALID_UPDATE_PREFERENCES, detail=str(exc))
    if prefs.mode is UpdateMode.OFF and not manual_check:
        return UpdateDecision(DecisionReason.UPDATE_MODE_OFF)
    if isinstance(manifest, ManifestValidationResult):
        parsed = manifest
    elif isinstance(manifest, ReleaseManifest):
        parsed = ManifestValidationResult(manifest)
    else:
        parsed = parse_release_manifest(manifest)
    if parsed.manifest is None:
        return UpdateDecision(parsed.reason or DecisionReason.INVALID_MANIFEST, detail=parsed.detail)
    try:
        installed = SemanticVersion.parse(installed_version)
    except UpdateValidationError as exc:
        return UpdateDecision(DecisionReason.INVALID_INSTALLED_VERSION, parsed.manifest, str(exc))
    remote = parsed.manifest.version
    if remote.compare(installed) <= 0:
        return UpdateDecision(DecisionReason.REMOTE_NOT_NEWER, parsed.manifest)
    try:
        updater = SemanticVersion.parse(updater_version)
    except UpdateValidationError as exc:
        return UpdateDecision(
            DecisionReason.UPDATER_INCOMPATIBLE,
            parsed.manifest,
            f"updater version unavailable or invalid: {exc}",
        )
    if updater.compare(parsed.manifest.minimum_updater_version) < 0:
        return UpdateDecision(DecisionReason.UPDATER_INCOMPATIBLE, parsed.manifest, "updater version is below manifest minimum")
    if prefs.ignore_patch_updates and remote.major == installed.major and remote.minor == installed.minor:
        return UpdateDecision(DecisionReason.PATCH_UPDATE_IGNORED, parsed.manifest)
    if prefs.skipped_version == str(remote) and not manual_check:
        return UpdateDecision(DecisionReason.VERSION_SKIPPED, parsed.manifest)
    return UpdateDecision(DecisionReason.UPDATE_AVAILABLE, parsed.manifest)
