from __future__ import annotations

import hashlib
import io
from pathlib import Path
import stat
import zipfile

import pytest

from hamchat.system_update_executor import SystemUpdateStatus, install_verified_candidate
from hamchat.update_acquisition import (
    AcquisitionCode,
    AcquisitionRequest,
    acquire_verified_candidate,
)
from hamchat.updates import (
    UpdatePreferences,
    decide_update,
    parse_release_manifest,
    release_manifest_digest,
)


class Response:
    def __init__(self, payload: bytes, headers: dict[str, str] | None = None):
        self._stream = io.BytesIO(payload)
        self.status = 200
        self.headers = headers or {"Content-Length": str(len(payload))}

    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def read(self, size: int) -> bytes: return self._stream.read(size)


class Transport:
    def __init__(self, response: Response | BaseException): self.response = response; self.calls = []
    def open(self, url: str, timeout: float):
        self.calls.append((url, timeout))
        if isinstance(self.response, BaseException): raise self.response
        return self.response


def archive(entries: dict[str, bytes], *, special: str | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name, content in entries.items():
            info = zipfile.ZipInfo(name)
            if special:
                info.external_attr = (special == "symlink") and ((stat.S_IFLNK | 0o777) << 16) or ((stat.S_IFIFO | 0o600) << 16)
            zipped.writestr(info, content)
    return output.getvalue()


def manifest_for(payload: bytes, files: dict[str, bytes], **change):
    value = {
        "schema_version": 2, "version": "2.7.0", "git_ref": "v2.7.0",
        "release_notes": "updates/2.7.0.md",
        "data_compatibility": {"database_schema_version": "2026-08-03.2", "data_layout_version": 1, "data_mutation_required": False},
        "release_payload": {
            "url": "https://github.com/hamwisk/HamChat/archive/v2.7.0.zip", "format": "zip",
            "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
            "root_prefix": "HamChat-v2.7.0",
            "files": [{"path": path, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()} for path, content in files.items()],
            "removals": [],
        },
    }
    value.update(change)
    parsed = parse_release_manifest(value)
    assert parsed.manifest is not None, parsed
    return parsed.manifest


def request(tmp_path: Path, manifest, *, transaction="txn-0000001", **change):
    install = tmp_path / "install"; install.mkdir(exist_ok=True)
    data = install / "data"; data.mkdir(exist_ok=True)
    decision = decide_update("2.6.0", manifest, UpdatePreferences())
    values = dict(decision=decision, manifest_digest=release_manifest_digest(manifest), installation_root=install, data_root=data, transaction_root=tmp_path / transaction, transaction_id=transaction)
    values.update(change)
    return AcquisitionRequest(**values)


def test_acquires_stages_binds_and_hands_off_to_executor(tmp_path):
    files = {"hamchat/a.py": b"new", "main.py": b"entry"}
    payload = archive({"HamChat-v2.7.0/" + path: content for path, content in files.items()} | {"HamChat-v2.7.0/README.md": b"not managed"})
    manifest = manifest_for(payload, files)
    install = tmp_path / "install"; install.mkdir(); (install / "hamchat").mkdir(); (install / "hamchat/a.py").write_bytes(b"old")
    data = install / "data"; data.mkdir(); sentinel = data / "sentinel"; sentinel.write_bytes(b"user")
    req = request(tmp_path, manifest)
    result = acquire_verified_candidate(req, transport=Transport(Response(payload)))
    assert result.succeeded and result.candidate is not None
    candidate = result.candidate
    assert sorted(path for path, _ in candidate.artifacts) == sorted(files)
    assert not (candidate.staging_root / "README.md").exists()
    installed = install_verified_candidate(candidate=candidate, installation_root=install, data_root=data, transaction_root=req.transaction_root)
    assert installed.status is SystemUpdateStatus.INSTALLED
    assert (install / "hamchat/a.py").read_bytes() == b"new"
    assert sentinel.read_bytes() == b"user"


@pytest.mark.parametrize(
    ("entries", "code"),
    [
        ({"HamChat-v2.7.0/hamchat/a.py": b"x", "HamChat-v2.7.0/../escape": b"x"}, AcquisitionCode.UNSAFE_ARCHIVE_MEMBER),
        ({"HamChat-v2.7.0/hamchat/a.py": b"x", "other/a.py": b"x"}, AcquisitionCode.UNSAFE_ARCHIVE_MEMBER),
        ({"HamChat-v2.7.0/hamchat/a.py": b"x", "HamChat-v2.7.0/C:/escape": b"x"}, AcquisitionCode.UNSAFE_ARCHIVE_MEMBER),
        ({"HamChat-v2.7.0/hamchat/a.py": b"x", "HamChat-v2.7.0/data/escape": b"x"}, AcquisitionCode.UNSAFE_ARCHIVE_MEMBER),
        ({"HamChat-v2.7.0/hamchat/a.py": b"x", "HamChat-v2.7.0/hamchat/A.py": b"x"}, AcquisitionCode.PATH_COLLISION),
    ],
)
def test_archive_attacks_are_rejected_without_staging(tmp_path, entries, code):
    payload = archive(entries)
    manifest = manifest_for(payload, {"hamchat/a.py": b"x"})
    req = request(tmp_path, manifest)
    result = acquire_verified_candidate(req, transport=Transport(Response(payload)))
    assert result.failure and result.failure.code is code and result.candidate is None
    assert not (req.transaction_root / "staged-release").exists()


@pytest.mark.parametrize("response, expected", [
    (TimeoutError(), AcquisitionCode.DOWNLOAD_TIMEOUT),
    (Response(b"x", {"Content-Length": "99"}), AcquisitionCode.PAYLOAD_SIZE_MISMATCH),
])
def test_download_failures_are_non_authorizing(tmp_path, response, expected):
    payload = archive({"HamChat-v2.7.0/hamchat/a.py": b"x"})
    manifest = manifest_for(payload, {"hamchat/a.py": b"x"})
    result = acquire_verified_candidate(request(tmp_path, manifest), transport=Transport(response))
    assert result.candidate is None and result.failure and result.failure.code is expected
    assert not result.source_mutation_permitted and not result.user_data_mutation_permitted


def test_truncated_excess_and_hard_limited_payloads_are_refused(tmp_path):
    payload = archive({"HamChat-v2.7.0/hamchat/a.py": b"x"})
    manifest = manifest_for(payload, {"hamchat/a.py": b"x"})
    for body, limit in ((payload[:-1], None), (payload + b"!", None), (payload, len(payload) - 1)):
        kwargs = {"max_archive_bytes": limit} if limit is not None else {}
        result = acquire_verified_candidate(request(tmp_path, manifest, transaction=f"txn-{len(body):07d}", **kwargs), transport=Transport(Response(body, {"Content-Length": str(len(payload))})))
        assert result.candidate is None
        assert result.failure and result.failure.code in {AcquisitionCode.PAYLOAD_SIZE_MISMATCH, AcquisitionCode.PAYLOAD_TOO_LARGE}


def test_digest_mismatch_and_user_owned_inventory_are_refused(tmp_path):
    payload = archive({"HamChat-v2.7.0/hamchat/a.py": b"x"})
    manifest = manifest_for(payload, {"hamchat/a.py": b"x"})
    value = {"schema_version": 2, "version": "2.7.0", "git_ref": "v2.7.0", "release_notes": "updates/2.7.0.md", "data_compatibility": {"database_schema_version": "2026-08-03.2", "data_layout_version": 1, "data_mutation_required": False}, "release_payload": {"url": "https://github.com/hamwisk/HamChat/archive/v2.7.0.zip", "format": "zip", "size": len(payload), "sha256": "0" * 64, "root_prefix": "HamChat-v2.7.0", "files": [{"path": "hamchat/a.py", "size": 1, "sha256": hashlib.sha256(b"x").hexdigest()}], "removals": []}}
    bad = parse_release_manifest(value).manifest
    assert bad is not None
    assert acquire_verified_candidate(request(tmp_path, bad), transport=Transport(Response(payload))).failure.code is AcquisitionCode.PAYLOAD_DIGEST_MISMATCH
    value["release_payload"]["files"][0]["path"] = "data/evil"
    assert parse_release_manifest(value).manifest is None


def test_manifest_and_transaction_binding_refuse_forged_request(tmp_path):
    payload = archive({"HamChat-v2.7.0/hamchat/a.py": b"x"})
    manifest = manifest_for(payload, {"hamchat/a.py": b"x"})
    req = request(tmp_path, manifest, transaction="txn-0000002", manifest_digest="f" * 64)
    result = acquire_verified_candidate(req, transport=Transport(Response(payload)))
    assert result.failure and result.failure.code is AcquisitionCode.TRANSACTION_MISMATCH
    assert not req.transaction_root.exists()


def test_staging_symlink_escape_and_redirect_are_rejected(tmp_path):
    payload = archive({"HamChat-v2.7.0/hamchat/a.py": b"x"})
    manifest = manifest_for(payload, {"hamchat/a.py": b"x"})
    outside = tmp_path / "outside"; outside.mkdir()
    link = tmp_path / "linked"; link.symlink_to(outside, target_is_directory=True)
    req = request(tmp_path, manifest, transaction="txn-0000003", transaction_root=link / "txn-0003")
    result = acquire_verified_candidate(req, transport=Transport(Response(payload)))
    assert result.failure and result.failure.code is AcquisitionCode.STAGING_INVALID
    redirected = Response(payload); redirected.status = 302
    result = acquire_verified_candidate(request(tmp_path, manifest, transaction="txn-0000003"), transport=Transport(redirected))
    assert result.failure and result.failure.code is AcquisitionCode.REDIRECT_REJECTED


def test_final_url_redirect_is_rejected(tmp_path):
    payload = archive({"HamChat-v2.7.0/hamchat/a.py": b"x"})
    manifest = manifest_for(payload, {"hamchat/a.py": b"x"})
    response = Response(payload)
    response.geturl = lambda: "https://evil.test/archive/v2.7.0.zip"
    result = acquire_verified_candidate(request(tmp_path, manifest), transport=Transport(response))
    assert result.failure and result.failure.code is AcquisitionCode.REDIRECT_REJECTED


def test_root_overlap_and_member_limit_are_refused(tmp_path):
    payload = archive({"HamChat-v2.7.0/hamchat/a.py": b"x"})
    manifest = manifest_for(payload, {"hamchat/a.py": b"x"})
    base = request(tmp_path, manifest)
    overlapped = AcquisitionRequest(**{**base.__dict__, "transaction_root": base.data_root / "txn-0000004", "transaction_id": "txn-0000004"})
    assert acquire_verified_candidate(overlapped, transport=Transport(Response(payload))).failure.code is AcquisitionCode.STAGING_INVALID
    limited = request(tmp_path, manifest, transaction="txn-0000005", max_member_bytes=0)
    assert acquire_verified_candidate(limited, transport=Transport(Response(payload))).failure.code is AcquisitionCode.RELEASE_METADATA_INVALID


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_unsafe_special_members_are_rejected_even_when_undeclared(tmp_path, kind):
    payload = archive({"HamChat-v2.7.0/hamchat/a.py": b"x", "HamChat-v2.7.0/ignored": b"x"}, special=kind)
    manifest = manifest_for(payload, {"hamchat/a.py": b"x"})
    result = acquire_verified_candidate(request(tmp_path, manifest), transport=Transport(Response(payload)))
    assert result.failure and result.failure.code is AcquisitionCode.UNSAFE_ARCHIVE_MEMBER


def test_staged_candidate_replacement_is_detected_by_real_executor(tmp_path):
    payload = archive({"HamChat-v2.7.0/hamchat/a.py": b"x"})
    manifest = manifest_for(payload, {"hamchat/a.py": b"x"})
    req = request(tmp_path, manifest)
    result = acquire_verified_candidate(req, transport=Transport(Response(payload)))
    assert result.candidate is not None
    staged = result.candidate.staging_root / "hamchat/a.py"
    staged.write_bytes(b"changed")
    blocked = install_verified_candidate(candidate=result.candidate, installation_root=req.installation_root, data_root=req.data_root, transaction_root=req.transaction_root)
    assert blocked.status is SystemUpdateStatus.BLOCKED
