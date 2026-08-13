# hamchat/updates.py
"""Pure manifest validation, preference handling, and update eligibility."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import logging
import re
from http.client import HTTPException
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .settings import save_settings
from .constants import DATA_LAYOUT_VERSION, SCHEMA_VERSION


_SEMVER = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_FIELDS = frozenset(
    ("schema_version", "version", "git_ref", "release_notes", "data_compatibility", "release_payload")
)
_SCHEMA_VERSION = 2
DEFAULT_MANIFEST_URL = (
    "https://raw.githubusercontent.com/hamwisk/HamChat/main/"
    "updates/latest.json"
)
DEFAULT_RELEASE_NOTES_BASE_URL = (
    "https://raw.githubusercontent.com/hamwisk/HamChat/"
)
MANIFEST_MAX_BYTES = 64 * 1024
RELEASE_NOTES_MAX_BYTES = 256 * 1024
UPDATE_REQUEST_TIMEOUT_SECONDS = 5.0
log = logging.getLogger("updates")


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
    DATA_COMPATIBILITY_BLOCKED = "data_compatibility_blocked"


class UpdateValidationError(ValueError):
    pass


@dataclass(frozen=True, eq=False)
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

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SemanticVersion) and self.compare(other) == 0

    def __hash__(self) -> int:
        return hash((self.major, self.minor, self.patch, self.prerelease))

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
        if not isinstance(value, Mapping):
            raise UpdateValidationError("updates settings must be an object")
        defaults = cls().as_mapping()
        merged = {key: value.get(key, default) for key, default in defaults.items()}
        try:
            mode = UpdateMode(merged["mode"])
        except (TypeError, ValueError) as exc:
            raise UpdateValidationError("invalid update mode") from exc
        return cls(mode, merged["ignore_patch_updates"], merged["skipped_version"])

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
    existing = settings.get("updates", {})
    update_settings = dict(existing) if isinstance(existing, Mapping) else {}
    update_settings.update(preferences.as_mapping())
    updated["updates"] = update_settings
    save_settings(path, updated)
    return updated


@dataclass(frozen=True)
class ReleaseManifest:
    schema_version: int
    version: SemanticVersion
    git_ref: str
    release_notes: str
    data_compatibility: "DataCompatibility"
    release_payload: "ReleasePayload"


@dataclass(frozen=True)
class DataCompatibility:
    database_schema_version: str
    data_layout_version: int
    data_mutation_required: bool

    @property
    def data_neutral(self) -> bool:
        return (self.database_schema_version == SCHEMA_VERSION
                and self.data_layout_version == DATA_LAYOUT_VERSION
                and not self.data_mutation_required)


@dataclass(frozen=True)
class ManagedReleaseFile:
    """One system-owned regular file in an immutable release payload."""

    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ReleasePayload:
    """Pinned archive and the exhaustive system-file inventory it may stage."""

    url: str
    archive_format: str
    size: int
    sha256: str
    root_prefix: str
    files: tuple[ManagedReleaseFile, ...]
    removals: tuple[str, ...] = ()


@dataclass(frozen=True)
class ManifestValidationResult:
    manifest: ReleaseManifest | None
    reason: DecisionReason | None = None
    detail: str | None = None


def _text(value: object, field: str) -> str:
    if (
        not isinstance(value, str) or not value or value != value.strip()
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise UpdateValidationError(f"{field} must be a non-empty whitespace-free string")
    return value


def _release_notes_path(value: object) -> str:
    text = _text(value, "release_notes")
    path = PurePosixPath(text)
    if ("\\" in text or "?" in text or "#" in text or ":" in text
            or path.as_posix() != text or path.is_absolute()
            or not path.parts or path.parts[0] != "updates"
            or any(part in {".", ".."} for part in path.parts)):
        raise UpdateValidationError("release_notes must be a safe repository-relative path under updates/")
    return text


def _git_ref(value: object) -> str:
    text = _text(value, "git_ref")
    if (
        text == "@" or text.startswith("/") or text.endswith(("/", "."))
        or ".." in text or "//" in text or "@{" in text
        or any(part in {".", ".."} or part.startswith(".") or part.endswith(".lock") for part in text.split("/"))
        or any(char in text for char in "~^:?*[\\")
    ):
        raise UpdateValidationError("git_ref is not a safe Git ref")
    return text


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise UpdateValidationError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _managed_path(value: object, field: str) -> str:
    text = _text(value, field)
    path = PurePosixPath(text)
    if ("\\" in text or ":" in text or path.as_posix() != text or path.is_absolute() or not path.parts
            or any(part in {".", ".."} for part in path.parts)
            or path.parts[0] in {"data", "settings", ".git", ".staging", "journals", "bundles"}):
        raise UpdateValidationError(f"{field} is not a managed system path")
    return text


def _release_payload(value: object, version: SemanticVersion, git_ref: str) -> ReleasePayload:
    if not isinstance(value, Mapping):
        raise UpdateValidationError("release_payload must be an object")
    required = {"url", "format", "size", "sha256", "root_prefix", "files", "removals"}
    if set(value) != required:
        raise UpdateValidationError("release_payload fields invalid")
    url = _valid_https_url(value["url"])
    if url is None:
        raise UpdateValidationError("release_payload URL must be clean HTTPS")
    parsed = urlsplit(url)
    # The manifest's immutable ref must be visible in the archive URL.  This
    # rejects mutable branch archives without hard-coding a single mirror.
    if not any(part == git_ref or part.startswith(f"{git_ref}.") for part in parsed.path.split("/")):
        raise UpdateValidationError("release_payload URL is not bound to git_ref")
    archive_format = value["format"]
    if archive_format != "zip":
        raise UpdateValidationError("unsupported release archive format")
    size = value["size"]
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= 1024 * 1024 * 1024:
        raise UpdateValidationError("release_payload size is invalid")
    root_prefix = _text(value["root_prefix"], "release_payload root_prefix")
    if "/" in root_prefix or "\\" in root_prefix or ":" in root_prefix or root_prefix in {".", ".."}:
        raise UpdateValidationError("release_payload root_prefix is invalid")
    files = value["files"]
    if not isinstance(files, list) or not files:
        raise UpdateValidationError("release_payload files must be a non-empty list")
    parsed_files: list[ManagedReleaseFile] = []
    seen: set[str] = set()
    seen_folded: set[str] = set()
    for item in files:
        if not isinstance(item, Mapping) or set(item) != {"path", "size", "sha256"}:
            raise UpdateValidationError("invalid release managed file")
        path = _managed_path(item["path"], "release_payload file path")
        file_size = item["size"]
        if isinstance(file_size, bool) or not isinstance(file_size, int) or file_size < 0:
            raise UpdateValidationError("release_payload file size is invalid")
        if path in seen or path.casefold() in seen_folded:
            raise UpdateValidationError("release_payload file paths collide")
        seen.add(path); seen_folded.add(path.casefold())
        parsed_files.append(ManagedReleaseFile(path, file_size, _sha256(item["sha256"], "release_payload file digest")))
    removals = value["removals"]
    if not isinstance(removals, list) or any(not isinstance(item, str) for item in removals):
        raise UpdateValidationError("release_payload removals must be a list")
    parsed_removals = tuple(_managed_path(item, "release_payload removal") for item in removals)
    if len(set(parsed_removals)) != len(parsed_removals) or set(parsed_removals) & seen:
        raise UpdateValidationError("release_payload removals collide")
    # The current executor has no removal operation.  Rejecting this in the
    # trusted manifest prevents absence from becoming implicit deletion.
    if parsed_removals:
        raise UpdateValidationError("release_payload removals are not supported")
    return ReleasePayload(url, archive_format, size, _sha256(value["sha256"], "release_payload digest"), root_prefix, tuple(parsed_files), parsed_removals)


def release_manifest_digest(manifest: ReleaseManifest) -> str:
    """Stable identity for binding a staged release to its trusted manifest."""
    payload = {
        "schema_version": manifest.schema_version, "version": str(manifest.version),
        "git_ref": manifest.git_ref, "release_notes": manifest.release_notes,
        "data_compatibility": manifest.data_compatibility.__dict__,
        "release_payload": {
            "url": manifest.release_payload.url, "format": manifest.release_payload.archive_format,
            "size": manifest.release_payload.size, "sha256": manifest.release_payload.sha256,
            "root_prefix": manifest.release_payload.root_prefix,
            "files": [file.__dict__ for file in manifest.release_payload.files],
            "removals": list(manifest.release_payload.removals),
        },
    }
    import hashlib
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


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
        if not release.is_stable:
            raise UpdateValidationError("manifest version must be a stable release")
        compatibility = value["data_compatibility"]
        if not isinstance(compatibility, Mapping) or set(compatibility) != {"database_schema_version", "data_layout_version", "data_mutation_required"}:
            raise UpdateValidationError("invalid data compatibility metadata")
        db_schema, layout, mutation = compatibility.values()
        if not isinstance(db_schema, str) or not db_schema or isinstance(layout, bool) or not isinstance(layout, int) or not isinstance(mutation, bool):
            raise UpdateValidationError("invalid data compatibility metadata")
        return ManifestValidationResult(
            ReleaseManifest(
                schema,
                release,
                _git_ref(value["git_ref"]),
                _release_notes_path(value["release_notes"]),
                DataCompatibility(db_schema, layout, mutation),
                _release_payload(value["release_payload"], release, _git_ref(value["git_ref"])),
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
    if not parsed.manifest.data_compatibility.data_neutral:
        return UpdateDecision(DecisionReason.DATA_COMPATIBILITY_BLOCKED, parsed.manifest)
    if remote.compare(installed) <= 0:
        return UpdateDecision(DecisionReason.REMOTE_NOT_NEWER, parsed.manifest)
    if prefs.ignore_patch_updates and remote.major == installed.major and remote.minor == installed.minor:
        return UpdateDecision(DecisionReason.PATCH_UPDATE_IGNORED, parsed.manifest)
    if prefs.skipped_version == str(remote) and not manual_check:
        return UpdateDecision(DecisionReason.VERSION_SKIPPED, parsed.manifest)
    return UpdateDecision(DecisionReason.UPDATE_AVAILABLE, parsed.manifest)


class RemoteCheckStatus(str, Enum):
    CHECKING_DISABLED = "checking_disabled"
    UPDATE_AVAILABLE_WITH_NOTES = "update_available_with_notes"
    NO_ELIGIBLE_UPDATE = "no_eligible_update"
    MANIFEST_URL_REJECTED = "manifest_url_rejected"
    MANIFEST_TIMEOUT = "manifest_timeout"
    MANIFEST_NETWORK_ERROR = "manifest_network_error"
    MANIFEST_HTTP_ERROR = "manifest_http_error"
    MANIFEST_TOO_LARGE = "manifest_too_large"
    MANIFEST_DECODING_ERROR = "manifest_decoding_error"
    MANIFEST_JSON_ERROR = "manifest_json_error"
    MANIFEST_INVALID = "manifest_invalid"
    RELEASE_NOTES_URL_REJECTED = "release_notes_url_rejected"
    RELEASE_NOTES_TIMEOUT = "release_notes_timeout"
    RELEASE_NOTES_NETWORK_ERROR = "release_notes_network_error"
    RELEASE_NOTES_HTTP_ERROR = "release_notes_http_error"
    RELEASE_NOTES_TOO_LARGE = "release_notes_too_large"
    RELEASE_NOTES_DECODING_ERROR = "release_notes_decoding_error"


@dataclass(frozen=True)
class RemoteUpdateResult:
    status: RemoteCheckStatus
    decision: UpdateDecision
    manifest: ReleaseManifest | None = None
    release_notes: str | None = None
    diagnostic: str | None = None


class _NoRedirectHandler(HTTPRedirectHandler):
    """Convert every redirect into a controlled HTTP failure."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibTransport:
    """Small injectable HTTPS transport with redirects disabled."""

    def __init__(self) -> None:
        self._opener = build_opener(_NoRedirectHandler())

    def open(self, url: str, timeout: float):
        request = Request(url, headers={"Accept": "application/json, text/markdown;q=0.9"})
        return self._opener.open(request, timeout=timeout)


def _valid_https_url(value: object) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https" or not parsed.netloc or parsed.username is not None
        or parsed.password is not None or parsed.query or parsed.fragment
    ):
        return None
    return value


def _release_notes_url(base_url: str, manifest: ReleaseManifest) -> str | None:
    base = _valid_https_url(base_url)
    if base is None:
        return None
    # git_ref and release_notes have already been syntactically restricted.
    return f"{base.rstrip('/')}/{manifest.git_ref}/{manifest.release_notes}"


def _read_limited(response: Any, limit: int) -> bytes:
    content_length = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
    expected_length: int | None = None
    if content_length is not None:
        try:
            expected_length = int(content_length)
            if expected_length > limit:
                raise OverflowError("declared payload exceeds limit")
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(8192, limit + 1 - total))
        if not chunk:
            if expected_length is not None and total != expected_length:
                raise OSError("truncated response body")
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise OverflowError("payload exceeds limit")
        chunks.append(chunk)


def _fetch_bytes(transport: Any, url: str, limit: int, timeout: float) -> tuple[bytes | None, RemoteCheckStatus | None, str | None]:
    try:
        with transport.open(url, timeout) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            if not 200 <= status < 300:
                return None, RemoteCheckStatus.MANIFEST_HTTP_ERROR, f"http_status={status}"
            return _read_limited(response, limit), None, None
    except TimeoutError:
        return None, RemoteCheckStatus.MANIFEST_TIMEOUT, "timeout"
    except HTTPError as exc:
        return None, RemoteCheckStatus.MANIFEST_HTTP_ERROR, f"http_status={exc.code}"
    except OverflowError:
        return None, RemoteCheckStatus.MANIFEST_TOO_LARGE, "payload_too_large"
    except (HTTPException, OSError, URLError):
        return None, RemoteCheckStatus.MANIFEST_NETWORK_ERROR, "network_error"


def _notes_status(status: RemoteCheckStatus) -> RemoteCheckStatus:
    return {
        RemoteCheckStatus.MANIFEST_TIMEOUT: RemoteCheckStatus.RELEASE_NOTES_TIMEOUT,
        RemoteCheckStatus.MANIFEST_NETWORK_ERROR: RemoteCheckStatus.RELEASE_NOTES_NETWORK_ERROR,
        RemoteCheckStatus.MANIFEST_HTTP_ERROR: RemoteCheckStatus.RELEASE_NOTES_HTTP_ERROR,
        RemoteCheckStatus.MANIFEST_TOO_LARGE: RemoteCheckStatus.RELEASE_NOTES_TOO_LARGE,
    }[status]


def check_for_updates(
    installed_version: object,
    preferences: UpdatePreferences | Mapping[str, Any] | object,
    *,
    manual_check: bool = False,
    manifest_url: str = DEFAULT_MANIFEST_URL,
    release_notes_base_url: str = DEFAULT_RELEASE_NOTES_BASE_URL,
    transport: Any = None,
    timeout: float = UPDATE_REQUEST_TIMEOUT_SECONDS,
) -> RemoteUpdateResult:
    """Fetch, validate, and decide updates; all failures are non-fatal results."""
    try:
        prefs = preferences if isinstance(preferences, UpdatePreferences) else UpdatePreferences.from_mapping(preferences)
    except UpdateValidationError as exc:
        return RemoteUpdateResult(
            RemoteCheckStatus.NO_ELIGIBLE_UPDATE,
            UpdateDecision(DecisionReason.INVALID_UPDATE_PREFERENCES, detail=str(exc)),
            diagnostic="invalid_preferences",
        )
    if prefs.mode is UpdateMode.OFF and not manual_check:
        return RemoteUpdateResult(
            RemoteCheckStatus.CHECKING_DISABLED,
            UpdateDecision(DecisionReason.UPDATE_MODE_OFF),
        )
    url = _valid_https_url(manifest_url)
    if url is None:
        return RemoteUpdateResult(
            RemoteCheckStatus.MANIFEST_URL_REJECTED,
            UpdateDecision(DecisionReason.INVALID_MANIFEST, detail="manifest URL must use HTTPS"),
            diagnostic="manifest_url_rejected",
        )
    transport = transport or UrllibTransport()
    payload, fetch_status, diagnostic = _fetch_bytes(transport, url, MANIFEST_MAX_BYTES, timeout)
    if fetch_status is not None:
        return RemoteUpdateResult(fetch_status, UpdateDecision(DecisionReason.INVALID_MANIFEST), diagnostic=diagnostic)
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError:
        return RemoteUpdateResult(RemoteCheckStatus.MANIFEST_DECODING_ERROR, UpdateDecision(DecisionReason.INVALID_MANIFEST), diagnostic="invalid_utf8")
    try:
        raw_manifest = json.loads(decoded)
    except json.JSONDecodeError:
        return RemoteUpdateResult(RemoteCheckStatus.MANIFEST_JSON_ERROR, UpdateDecision(DecisionReason.INVALID_MANIFEST), diagnostic="invalid_json")
    parsed = parse_release_manifest(raw_manifest)
    decision = decide_update(installed_version, parsed, prefs, manual_check=manual_check)
    if parsed.manifest is None:
        safe_decision = UpdateDecision(
            parsed.reason or DecisionReason.INVALID_MANIFEST,
            detail="manifest_validation_failed",
        )
        return RemoteUpdateResult(
            RemoteCheckStatus.MANIFEST_INVALID,
            safe_decision,
            diagnostic="manifest_validation_failed",
        )
    if not decision.update_available:
        return RemoteUpdateResult(RemoteCheckStatus.NO_ELIGIBLE_UPDATE, decision, parsed.manifest)
    notes_url = _release_notes_url(release_notes_base_url, parsed.manifest)
    if notes_url is None:
        return RemoteUpdateResult(RemoteCheckStatus.RELEASE_NOTES_URL_REJECTED, decision, parsed.manifest, diagnostic="release_notes_url_rejected")
    payload, fetch_status, diagnostic = _fetch_bytes(transport, notes_url, RELEASE_NOTES_MAX_BYTES, timeout)
    if fetch_status is not None:
        return RemoteUpdateResult(_notes_status(fetch_status), decision, parsed.manifest, diagnostic=diagnostic)
    try:
        notes = payload.decode("utf-8")
    except UnicodeDecodeError:
        return RemoteUpdateResult(RemoteCheckStatus.RELEASE_NOTES_DECODING_ERROR, decision, parsed.manifest, diagnostic="invalid_utf8")
    log.info("Update release notes retrieved version=%s byte_count=%d", parsed.manifest.version, len(payload))
    return RemoteUpdateResult(RemoteCheckStatus.UPDATE_AVAILABLE_WITH_NOTES, decision, parsed.manifest, notes)
