"""Acquire and stage one manifest-pinned system-only release archive.

This boundary deliberately owns download and archive handling.  UI code only
receives an :class:`~hamchat.system_update_executor.VerifiedStagedCandidate`.
Nothing in this module writes below the installation or configured data root.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import logging
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
import zipfile

from .system_update_executor import VerifiedStagedCandidate, prepare_system_install
from .updates import DecisionReason, ReleaseManifest, UpdateDecision, release_manifest_digest


log = logging.getLogger("updates")
_CHUNK = 1024 * 1024
_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


class AcquisitionCode(str, Enum):
    INELIGIBLE_CANDIDATE = "ineligible_candidate"
    RELEASE_METADATA_INVALID = "release_metadata_invalid"
    UNSAFE_RELEASE_SOURCE = "unsafe_release_source"
    NETWORK_FAILURE = "network_failure"
    DOWNLOAD_TIMEOUT = "download_timeout"
    REDIRECT_REJECTED = "redirect_rejected"
    PAYLOAD_SIZE_MISMATCH = "payload_size_mismatch"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    PAYLOAD_DIGEST_MISMATCH = "payload_digest_mismatch"
    UNSUPPORTED_ARCHIVE = "unsupported_archive"
    UNSAFE_ARCHIVE_MEMBER = "unsafe_archive_member"
    PATH_COLLISION = "path_collision"
    MANAGED_FILE_MISSING = "managed_file_missing"
    MANAGED_FILE_MISMATCH = "managed_file_mismatch"
    UNSAFE_MANAGED_PATH = "unsafe_managed_path"
    STAGING_INVALID = "staging_invalid"
    STAGING_DURABILITY_FAILURE = "staging_durability_failure"
    TRANSACTION_MISMATCH = "transaction_mismatch"
    JOURNAL_PREPARATION_FAILURE = "journal_preparation_failure"
    STAGED_CANDIDATE_CHANGED = "staged_candidate_changed"


@dataclass(frozen=True)
class AcquisitionFailure:
    code: AcquisitionCode
    context: str | None = None


@dataclass(frozen=True)
class AcquisitionRequest:
    decision: UpdateDecision
    manifest_digest: str
    installation_root: Path
    data_root: Path
    transaction_root: Path
    transaction_id: str
    max_archive_bytes: int = 1024 * 1024 * 1024
    max_members: int = 10_000
    max_member_bytes: int = 1024 * 1024 * 1024
    max_total_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024
    timeout_seconds: float = 15.0


@dataclass(frozen=True)
class AcquisitionResult:
    candidate: VerifiedStagedCandidate | None
    failure: AcquisitionFailure | None = None
    source_mutation_permitted: bool = False
    user_data_mutation_permitted: bool = False

    @property
    def succeeded(self) -> bool:
        return self.candidate is not None and self.failure is None


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _has_symlink_component(path: Path) -> bool:
    """Reject a caller-selected staging root hidden behind a link."""
    current = path
    while current != current.parent:
        if current.exists() and current.is_symlink():
            return True
        current = current.parent
    return False


def _valid_transaction_id(value: str) -> bool:
    return 8 <= len(value) <= 128 and value[0].isalnum() and all(char in _ID_CHARS for char in value)


def _digest_stream(stream: Any, destination: Any | None = None, limit: int | None = None) -> tuple[int, str]:
    digest = hashlib.sha256(); size = 0
    while True:
        chunk = stream.read(_CHUNK)
        if not chunk:
            break
        size += len(chunk)
        if limit is not None and size > limit:
            raise OverflowError
        digest.update(chunk)
        if destination is not None:
            destination.write(chunk)
    return size, digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _safe_member_name(name: str, prefix: str) -> str | None:
    if not name or "\x00" in name or "\\" in name or name.startswith(("/", "\\")):
        return None
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    if path.parts[0] != prefix:
        return None
    if len(path.parts) == 1:
        return ""
    relative = "/".join(path.parts[1:])
    # This validates archive structure only.  Authorization remains at the
    # manifest boundary: ``_managed_path`` forbids protected roots such as
    # ``settings`` and ``data`` from the trusted installation inventory.  A
    # normal tagged source ZIP may legitimately contain undeclared regular
    # files below those roots; they are inspected but never staged.
    if any(":" in part for part in path.parts):
        return None
    return relative


def _zip_member_kind(info: zipfile.ZipInfo) -> str:
    mode = info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if info.is_dir() or info.filename.endswith("/"):
        return "directory"
    # Zip files produced on Windows commonly have no POSIX type bits; regular
    # entries with mode zero are accepted, every explicit special type is not.
    if kind in {0, stat.S_IFREG}:
        return "regular"
    return "special"


def _validate_request(request: AcquisitionRequest) -> tuple[ReleaseManifest | None, AcquisitionFailure | None]:
    decision = request.decision
    manifest = decision.manifest
    if decision.reason is not DecisionReason.UPDATE_AVAILABLE or manifest is None:
        return None, AcquisitionFailure(AcquisitionCode.INELIGIBLE_CANDIDATE)
    if not manifest.data_compatibility.data_neutral:
        return None, AcquisitionFailure(AcquisitionCode.RELEASE_METADATA_INVALID, "data_compatibility")
    if request.manifest_digest != release_manifest_digest(manifest):
        return None, AcquisitionFailure(AcquisitionCode.TRANSACTION_MISMATCH, "manifest")
    if not _valid_transaction_id(request.transaction_id):
        return None, AcquisitionFailure(AcquisitionCode.TRANSACTION_MISMATCH, "transaction")
    if (request.max_archive_bytes <= 0 or request.max_members <= 0
            or request.max_member_bytes <= 0 or request.max_total_uncompressed_bytes <= 0):
        return None, AcquisitionFailure(AcquisitionCode.RELEASE_METADATA_INVALID, "limits")
    payload = manifest.release_payload
    parsed = urlsplit(payload.url)
    try:
        permitted_port = parsed.port in {None, 443}
    except ValueError:
        permitted_port = False
    if (parsed.scheme != "https" or not permitted_port
            or parsed.hostname not in {"github.com", "codeload.github.com"}):
        return None, AcquisitionFailure(AcquisitionCode.UNSAFE_RELEASE_SOURCE)
    if payload.size > request.max_archive_bytes:
        return None, AcquisitionFailure(AcquisitionCode.PAYLOAD_TOO_LARGE)
    supplied_transaction_root = Path(request.transaction_root)
    roots = tuple(Path(item).resolve(strict=False) for item in (request.installation_root, request.data_root, supplied_transaction_root))
    if (not supplied_transaction_root.is_absolute() or _has_symlink_component(supplied_transaction_root)
            or any(not root.is_absolute() for root in roots) or _within(roots[2], roots[0]) or _within(roots[2], roots[1])):
        return None, AcquisitionFailure(AcquisitionCode.STAGING_INVALID, "root_overlap")
    return manifest, None


def _download(*, transport: Any, url: str, expected_size: int, expected_digest: str, destination: Path, limit: int, timeout: float) -> AcquisitionFailure | None:
    try:
        with transport.open(url, timeout) as response:
            final_url = response.geturl() if hasattr(response, "geturl") else url
            if final_url != url:
                return AcquisitionFailure(AcquisitionCode.REDIRECT_REJECTED)
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            if not 200 <= status < 300:
                return AcquisitionFailure(AcquisitionCode.REDIRECT_REJECTED if 300 <= status < 400 else AcquisitionCode.NETWORK_FAILURE, "http_status")
            headers = getattr(response, "headers", {}) or {}
            declared = headers.get("Content-Length")
            if declared is not None:
                try:
                    if int(declared) != expected_size:
                        return AcquisitionFailure(AcquisitionCode.PAYLOAD_SIZE_MISMATCH, "content_length")
                except ValueError:
                    return AcquisitionFailure(AcquisitionCode.PAYLOAD_SIZE_MISMATCH, "content_length")
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as output:
                    size, digest = _digest_stream(response, output, limit)
                    output.flush(); os.fsync(output.fileno())
            finally:
                # fd is owned by fdopen on the successful path.
                pass
        _fsync_directory(destination.parent)
    except TimeoutError:
        return AcquisitionFailure(AcquisitionCode.DOWNLOAD_TIMEOUT)
    except HTTPError as exc:
        return AcquisitionFailure(AcquisitionCode.REDIRECT_REJECTED if exc.code in {301, 302, 303, 307, 308} else AcquisitionCode.NETWORK_FAILURE, "http_error")
    except (OSError, URLError):
        return AcquisitionFailure(AcquisitionCode.NETWORK_FAILURE)
    except OverflowError:
        return AcquisitionFailure(AcquisitionCode.PAYLOAD_TOO_LARGE)
    if size != expected_size:
        return AcquisitionFailure(AcquisitionCode.PAYLOAD_SIZE_MISMATCH)
    if digest != expected_digest:
        return AcquisitionFailure(AcquisitionCode.PAYLOAD_DIGEST_MISMATCH)
    try:
        with destination.open("rb") as source:
            size2, digest2 = _digest_stream(source, limit=limit)
    except OSError:
        return AcquisitionFailure(AcquisitionCode.STAGING_DURABILITY_FAILURE, "download")
    if (size2, digest2) != (size, digest):
        return AcquisitionFailure(AcquisitionCode.PAYLOAD_DIGEST_MISMATCH)
    return None


def _scan_archive(archive: Path, manifest: ReleaseManifest, request: AcquisitionRequest) -> tuple[dict[str, zipfile.ZipInfo] | None, AcquisitionFailure | None]:
    try:
        with zipfile.ZipFile(archive) as zipped:
            infos = zipped.infolist()
            if len(infos) > request.max_members:
                return None, AcquisitionFailure(AcquisitionCode.UNSAFE_ARCHIVE_MEMBER, "member_count")
            files: dict[str, zipfile.ZipInfo] = {}; names: set[str] = set(); folded: set[str] = set(); total = 0
            for info in infos:
                relative = _safe_member_name(info.filename, manifest.release_payload.root_prefix)
                if relative is None:
                    return None, AcquisitionFailure(AcquisitionCode.UNSAFE_ARCHIVE_MEMBER)
                kind = _zip_member_kind(info)
                if kind == "special":
                    return None, AcquisitionFailure(AcquisitionCode.UNSAFE_ARCHIVE_MEMBER, "special")
                if relative == "":
                    if kind != "directory":
                        return None, AcquisitionFailure(AcquisitionCode.UNSAFE_ARCHIVE_MEMBER)
                    continue
                if info.file_size < 0 or info.file_size > request.max_member_bytes:
                    return None, AcquisitionFailure(AcquisitionCode.UNSAFE_ARCHIVE_MEMBER, "member_size")
                total += info.file_size
                if total > request.max_total_uncompressed_bytes:
                    return None, AcquisitionFailure(AcquisitionCode.UNSAFE_ARCHIVE_MEMBER, "aggregate_size")
                if relative in names or relative.casefold() in folded:
                    return None, AcquisitionFailure(AcquisitionCode.PATH_COLLISION)
                path_parts = relative.split("/")
                if any("/".join(path_parts[:index]) in files for index in range(1, len(path_parts))):
                    return None, AcquisitionFailure(AcquisitionCode.PATH_COLLISION, "file_directory")
                if kind == "regular" and any(name.startswith(relative + "/") for name in names):
                    return None, AcquisitionFailure(AcquisitionCode.PATH_COLLISION, "file_directory")
                names.add(relative); folded.add(relative.casefold())
                if kind == "regular":
                    files[relative] = info
            expected = {file.path for file in manifest.release_payload.files}
            if not expected.issubset(files):
                return None, AcquisitionFailure(AcquisitionCode.MANAGED_FILE_MISSING)
            return {path: files[path] for path in expected}, None
    except (OSError, zipfile.BadZipFile, NotImplementedError):
        return None, AcquisitionFailure(AcquisitionCode.UNSUPPORTED_ARCHIVE)


def _stage(archive: Path, manifest: ReleaseManifest, transaction_root: Path, request: AcquisitionRequest) -> tuple[Path | None, AcquisitionFailure | None]:
    members, failure = _scan_archive(archive, manifest, request)
    if failure:
        return None, failure
    staging = transaction_root / "staged-release"
    try:
        staging.mkdir(mode=0o700)
        os.chmod(staging, 0o700)
        expected = {item.path: item for item in manifest.release_payload.files}
        with zipfile.ZipFile(archive) as zipped:
            for path in sorted(expected):
                item, info = expected[path], members[path]
                target = staging.joinpath(*PurePosixPath(path).parts)
                if not _within(target, staging):
                    return None, AcquisitionFailure(AcquisitionCode.STAGING_INVALID, "containment")
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                temporary = target.with_name(f".{target.name}.part")
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    with os.fdopen(fd, "wb") as output, zipped.open(info, "r") as source:
                        size, digest = _digest_stream(source, output, item.size)
                        output.flush(); os.fsync(output.fileno())
                except Exception:
                    if temporary.exists():
                        temporary.unlink()
                    raise
                if (size, digest) != (item.size, item.sha256):
                    temporary.unlink(missing_ok=True)
                    return None, AcquisitionFailure(AcquisitionCode.MANAGED_FILE_MISMATCH)
                os.replace(temporary, target); os.chmod(target, 0o600); _fsync_directory(target.parent)
        for path, item in expected.items():
            target = staging.joinpath(*PurePosixPath(path).parts)
            if not target.is_file() or target.is_symlink():
                return None, AcquisitionFailure(AcquisitionCode.STAGED_CANDIDATE_CHANGED)
            with target.open("rb") as source:
                size, digest = _digest_stream(source, limit=item.size)
            if (size, digest) != (item.size, item.sha256):
                return None, AcquisitionFailure(AcquisitionCode.STAGED_CANDIDATE_CHANGED)
        _fsync_directory(staging)
        return staging, None
    except OverflowError:
        return None, AcquisitionFailure(AcquisitionCode.MANAGED_FILE_MISMATCH)
    except OSError:
        return None, AcquisitionFailure(AcquisitionCode.STAGING_DURABILITY_FAILURE)


def acquire_verified_candidate(request: AcquisitionRequest, *, transport: Any) -> AcquisitionResult:
    """Create one journal-bound staged candidate from trusted manifest metadata."""
    manifest, failure = _validate_request(request)
    if failure:
        log.warning("Update acquisition refused code=%s", failure.code.value)
        return AcquisitionResult(None, failure)
    assert manifest is not None
    root = request.transaction_root.resolve(strict=False)
    log.info("Update acquisition started version=%s", manifest.version)
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
        os.chmod(root, 0o700)
    except FileExistsError:
        return AcquisitionResult(None, AcquisitionFailure(AcquisitionCode.TRANSACTION_MISMATCH, "existing_transaction"))
    except OSError:
        return AcquisitionResult(None, AcquisitionFailure(AcquisitionCode.STAGING_DURABILITY_FAILURE, "transaction_root"))
    archive = root / "release.zip"
    try:
        log.info("Update payload download started version=%s", manifest.version)
        failure = _download(transport=transport, url=manifest.release_payload.url, expected_size=manifest.release_payload.size, expected_digest=manifest.release_payload.sha256, destination=archive, limit=request.max_archive_bytes, timeout=request.timeout_seconds)
        if failure:
            return AcquisitionResult(None, failure)
        log.info("Update payload download completed version=%s", manifest.version)
        log.info("Update archive verified version=%s", manifest.version)
        log.info("Update staging started version=%s", manifest.version)
        staging, failure = _stage(archive, manifest, root, request)
        if failure:
            return AcquisitionResult(None, failure)
        assert staging is not None
        artifacts = tuple((file.path, file.sha256) for file in manifest.release_payload.files)
        candidate = VerifiedStagedCandidate(request.transaction_id, staging, manifest, artifacts, True)
        try:
            prepare_system_install(candidate, root)
        except (OSError, ValueError):
            return AcquisitionResult(None, AcquisitionFailure(AcquisitionCode.JOURNAL_PREPARATION_FAILURE))
        log.info("Update staged inventory verified version=%s", manifest.version)
        log.info("Verified staged candidate prepared version=%s", manifest.version)
        return AcquisitionResult(candidate)
    except Exception:
        log.exception("Unexpected update acquisition failure type=internal")
        return AcquisitionResult(None, AcquisitionFailure(AcquisitionCode.STAGING_DURABILITY_FAILURE, "internal"))
