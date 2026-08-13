from __future__ import annotations

import json
import hashlib
from urllib.error import HTTPError

import pytest

from hamchat.settings import load_settings
from hamchat.updates import (
    DecisionReason,
    RemoteCheckStatus,
    SemanticVersion,
    UpdateMode,
    UpdatePreferences,
    UpdateValidationError,
    check_for_updates,
    decide_update,
    parse_release_manifest,
    preferences_from_settings,
    save_update_preferences,
)


def manifest(version="2.7.0", **changes):
    payload = b"placeholder release payload"
    managed = b"placeholder managed file"
    value = {
        "schema_version": 2,
        "version": version,
        "git_ref": f"v{version}",
        "release_notes": "updates/2.7.0.md",
        "data_compatibility": {"database_schema_version": "2026-08-03.2", "data_layout_version": 1, "data_mutation_required": False},
        "release_payload": {
            "url": f"https://example.test/archive/v{version}.zip",
            "format": "zip",
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "root_prefix": f"HamChat-v{version}",
            "files": [{"path": "hamchat/example.py", "size": len(managed), "sha256": hashlib.sha256(managed).hexdigest()}],
            "removals": [],
        },
    }
    value.update(changes)
    return value


class FakeResponse:
    def __init__(self, payload=b"", status=200, headers=None, chunks=None):
        self.payload = payload
        self.status = status
        self.headers = headers or {}
        self.chunks = list(chunks) if chunks is not None else None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size):
        if self.chunks is not None:
            return self.chunks.pop(0) if self.chunks else b""
        payload, self.payload = self.payload[:size], self.payload[size:]
        return payload


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, url, timeout):
        self.calls.append((url, timeout))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def manifest_bytes(version="2.7.0", **changes):
    return json.dumps(manifest(version, **changes)).encode()


@pytest.mark.parametrize(
    ("installed", "remote", "reason"),
    [
        ("2.6.1", "2.6.2", DecisionReason.UPDATE_AVAILABLE),
        ("2.6.1", "2.7.0", DecisionReason.UPDATE_AVAILABLE),
        ("2.7.0", "2.7.0", DecisionReason.REMOTE_NOT_NEWER),
        ("2.7.0", "2.6.9", DecisionReason.REMOTE_NOT_NEWER),
        ("2.6.1", "3.0.0", DecisionReason.UPDATE_AVAILABLE),
        ("2.7.0-dev", "2.7.0", DecisionReason.UPDATE_AVAILABLE),
        ("2.7.1-dev", "2.7.0", DecisionReason.REMOTE_NOT_NEWER),
    ],
)
def test_semver_eligibility(installed, remote, reason):
    assert decide_update(installed, manifest(remote), UpdatePreferences()).reason is reason


def test_semver_build_metadata_does_not_affect_equality_or_precedence():
    left = SemanticVersion.parse("2.7.0+build.1")
    right = SemanticVersion.parse("2.7.0+build.2")
    assert left == right
    assert hash(left) == hash(right)
    assert left.compare(right) == 0


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("1.0.0-alpha", "1.0.0-alpha.1"),
        ("1.0.0-alpha.1", "1.0.0-alpha.beta"),
        ("1.0.0-alpha.beta", "1.0.0-beta"),
        ("1.0.0-1", "1.0.0-alpha"),
        ("1.0.0-999999999999999999999", "1.0.0-1000000000000000000000"),
    ],
)
def test_semver_prerelease_precedence_is_deterministic(left, right):
    assert SemanticVersion.parse(left).compare(SemanticVersion.parse(right)) == -1


@pytest.mark.parametrize(
    "value",
    ["01.0.0", "1.01.0", "1.0.01", "1.0.0-01", "1.0.0-", "1.0.0-alpha..1", "1.0.0-β"],
)
def test_semver_rejects_leading_zero_empty_and_non_ascii_identifiers(value):
    with pytest.raises(UpdateValidationError):
        SemanticVersion.parse(value)


@pytest.mark.parametrize(
    ("remote", "reason"),
    [
        ("2.6.2", DecisionReason.PATCH_UPDATE_IGNORED),
        ("2.7.0", DecisionReason.UPDATE_AVAILABLE),
        ("3.0.0", DecisionReason.UPDATE_AVAILABLE),
    ],
)
def test_patch_filter(remote, reason):
    preferences = UpdatePreferences(ignore_patch_updates=True)
    assert decide_update("2.6.1", manifest(remote), preferences).reason is reason


@pytest.mark.parametrize(
    ("mode", "manual", "reason"),
    [
        (UpdateMode.AUTOMATIC, False, DecisionReason.UPDATE_AVAILABLE),
        (UpdateMode.PROMPT, False, DecisionReason.UPDATE_AVAILABLE),
        (UpdateMode.OFF, False, DecisionReason.UPDATE_MODE_OFF),
        (UpdateMode.OFF, True, DecisionReason.UPDATE_AVAILABLE),
    ],
)
def test_modes(mode, manual, reason):
    assert decide_update("2.6.1", manifest(), UpdatePreferences(mode=mode), manual_check=manual).reason is reason


def test_skips_are_exact_and_manual_bypasses_only_the_skip():
    preferences = UpdatePreferences(skipped_version="2.7.0")
    assert decide_update("2.6.1", manifest(), preferences).reason is DecisionReason.VERSION_SKIPPED
    assert decide_update("2.6.1", manifest("2.7.1"), preferences).reason is DecisionReason.UPDATE_AVAILABLE
    assert decide_update("2.6.1", manifest(), preferences, manual_check=True).reason is DecisionReason.UPDATE_AVAILABLE
    assert decide_update("2.7.1", manifest(), preferences, manual_check=True).reason is DecisionReason.REMOTE_NOT_NEWER


@pytest.mark.parametrize("installed", ["2.7", "v2.7.0", 270, True])
def test_invalid_installed_versions_are_controlled(installed):
    assert decide_update(installed, manifest(), UpdatePreferences()).reason is DecisionReason.INVALID_INSTALLED_VERSION


@pytest.mark.parametrize("remote", ["2.7", "2.7.0-dev", "2.7.0-alpha.1", True])
def test_invalid_or_prerelease_remote_versions_are_rejected(remote):
    assert parse_release_manifest(manifest(remote)).reason is DecisionReason.INVALID_MANIFEST


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": None}, {"schema_version": True}, {"version": 2},
        {"git_ref": ""}, {"git_ref": "refs/../heads/x"}, {"git_ref": "v2.7.0?x"},
        {"git_ref": "refs\\heads\\x"}, {"git_ref": "refs/.hidden/x"},
        {"release_notes": "../secret.md"}, {"release_notes": "/updates/2.7.0.md"},
        {"release_notes": "updates/2.7.0.md?x"}, {"release_notes": "updates/./2.7.0.md"},
        {"release_notes": "updates\\2.7.0.md"},
    ],
)
def test_manifest_field_validation(change):
    assert parse_release_manifest(manifest(**change)).reason is DecisionReason.INVALID_MANIFEST


def test_manifest_structure_and_future_schema_are_controlled():
    incomplete = manifest()
    del incomplete["git_ref"]
    assert parse_release_manifest(incomplete).reason is DecisionReason.INVALID_MANIFEST
    assert parse_release_manifest(manifest(extra="no")).reason is DecisionReason.INVALID_MANIFEST
    assert parse_release_manifest(manifest(schema_version=3)).reason is DecisionReason.UNSUPPORTED_MANIFEST_SCHEMA


@pytest.mark.parametrize(
    "payload_change",
    [
        {"url": "http://example.test/archive/v2.7.0.zip"},
        {"url": "https://example.test/archive/main.zip"},
        {"format": "tar"},
        {"files": []},
        {"files": [{"path": "data/x", "size": 1, "sha256": "0" * 64}]},
        {"removals": ["hamchat/old.py"]},
    ],
)
def test_release_payload_descriptor_is_strict(payload_change):
    value = manifest()
    value["release_payload"].update(payload_change)
    assert parse_release_manifest(value).reason is DecisionReason.INVALID_MANIFEST


def test_preferences_accept_missing_and_unknown_keys_without_mutating_input():
    raw = {"mode": "automatic", "future_key": "preserved"}
    assert UpdatePreferences.from_mapping(raw).mode is UpdateMode.AUTOMATIC
    assert raw == {"mode": "automatic", "future_key": "preserved"}


@pytest.mark.parametrize(
    "raw",
    [
        None, {"mode": "bad"},
        {"mode": "prompt", "ignore_patch_updates": 1},
        {"skipped_version": "2.7.0-dev"},
    ],
)
def test_invalid_preferences_are_controlled(raw):
    assert decide_update("2.6.1", manifest(), raw).reason is DecisionReason.INVALID_UPDATE_PREFERENCES


def test_settings_defaults_and_persistence(tmp_path):
    path = tmp_path / "app.json"
    settings = load_settings(path)
    assert preferences_from_settings(settings) == UpdatePreferences()
    settings["updates"]["future_key"] = "preserve"
    save_update_preferences(path, settings, UpdatePreferences(mode=UpdateMode.AUTOMATIC, skipped_version="2.7.0"))
    stored = json.loads(path.read_text())
    assert stored["updates"]["mode"] == "automatic"
    assert stored["updates"]["future_key"] == "preserve"


def test_preference_model_requires_typed_mode():
    with pytest.raises(UpdateValidationError):
        UpdatePreferences(mode="prompt")


def test_successful_fetch_returns_notes_and_uses_immutable_release_ref():
    transport = FakeTransport([FakeResponse(manifest_bytes()), FakeResponse(b"# Notes")])
    result = check_for_updates("2.7.0-dev", UpdatePreferences(), manifest_url="https://example.test/latest.json", release_notes_base_url="https://example.test/repo", transport=transport)
    assert result.status is RemoteCheckStatus.UPDATE_AVAILABLE_WITH_NOTES
    assert result.decision.reason is DecisionReason.UPDATE_AVAILABLE
    assert result.release_notes == "# Notes"
    assert transport.calls[1][0] == "https://example.test/repo/v2.7.0/updates/2.7.0.md"


def test_non_newer_skipped_and_patch_filtered_updates_do_not_fetch_notes():
    cases = [
        ("2.7.0", "2.7.0", UpdatePreferences(), DecisionReason.REMOTE_NOT_NEWER),
        ("2.6.1", "2.7.0", UpdatePreferences(skipped_version="2.7.0"), DecisionReason.VERSION_SKIPPED),
        ("2.6.1", "2.6.2", UpdatePreferences(ignore_patch_updates=True), DecisionReason.PATCH_UPDATE_IGNORED),
    ]
    for installed, remote, preferences, reason in cases:
        transport = FakeTransport([FakeResponse(manifest_bytes(remote))])
        result = check_for_updates(installed, preferences, manifest_url="https://example.test/latest", transport=transport)
        assert result.status is RemoteCheckStatus.NO_ELIGIBLE_UPDATE
        assert result.decision.reason is reason
        assert len(transport.calls) == 1


def test_off_mode_performs_no_network_request_and_manual_bypasses_skip():
    transport = FakeTransport([])
    result = check_for_updates("2.6.1", UpdatePreferences(mode=UpdateMode.OFF), transport=transport)
    assert result.status is RemoteCheckStatus.CHECKING_DISABLED
    assert transport.calls == []
    transport = FakeTransport([FakeResponse(manifest_bytes()), FakeResponse(b"notes")])
    result = check_for_updates("2.6.1", UpdatePreferences(skipped_version="2.7.0"), manual_check=True, manifest_url="https://example.test/latest", release_notes_base_url="https://example.test/repo", transport=transport)
    assert result.status is RemoteCheckStatus.UPDATE_AVAILABLE_WITH_NOTES


@pytest.mark.parametrize(
    ("response", "status"),
    [
        (TimeoutError(), RemoteCheckStatus.MANIFEST_TIMEOUT),
        (OSError("dns"), RemoteCheckStatus.MANIFEST_NETWORK_ERROR),
        (FakeResponse(status=503), RemoteCheckStatus.MANIFEST_HTTP_ERROR),
        (FakeResponse(b"{"), RemoteCheckStatus.MANIFEST_JSON_ERROR),
        (FakeResponse(b"\xff"), RemoteCheckStatus.MANIFEST_DECODING_ERROR),
        (FakeResponse(manifest_bytes("2.7.0-dev")), RemoteCheckStatus.MANIFEST_INVALID),
        (FakeResponse(b"x", headers={"Content-Length": "999999"}), RemoteCheckStatus.MANIFEST_TOO_LARGE),
        (FakeResponse(chunks=[b"x" * (64 * 1024 + 1)]), RemoteCheckStatus.MANIFEST_TOO_LARGE),
        (FakeResponse(b"{}", headers={"Content-Length": "3"}), RemoteCheckStatus.MANIFEST_NETWORK_ERROR),
    ],
)
def test_manifest_failures_are_controlled(response, status):
    transport = FakeTransport([response])
    result = check_for_updates("2.6.1", UpdatePreferences(), manifest_url="https://example.test/latest", transport=transport)
    assert result.status is status
    assert result.release_notes is None


def test_invalid_manifest_diagnostics_do_not_include_remote_payload_values():
    transport = FakeTransport([FakeResponse(manifest_bytes(version="not-safe-to-log"))])
    result = check_for_updates("2.6.1", UpdatePreferences(), manifest_url="https://example.test/latest", transport=transport)
    assert result.status is RemoteCheckStatus.MANIFEST_INVALID
    assert result.diagnostic == "manifest_validation_failed"
    assert result.decision.detail == "manifest_validation_failed"


@pytest.mark.parametrize("url", ["http://example.test/latest", "https://example.test/latest?x=1", "file:///tmp/latest"])
def test_manifest_url_requires_clean_https_and_never_contacts_transport(url):
    transport = FakeTransport([])
    result = check_for_updates("2.6.1", UpdatePreferences(), manifest_url=url, transport=transport)
    assert result.status is RemoteCheckStatus.MANIFEST_URL_REJECTED
    assert transport.calls == []


def test_redirect_is_a_controlled_http_failure():
    transport = FakeTransport([HTTPError("https://example.test/latest", 302, "Found", {}, None)])
    result = check_for_updates("2.6.1", UpdatePreferences(), manifest_url="https://example.test/latest", transport=transport)
    assert result.status is RemoteCheckStatus.MANIFEST_HTTP_ERROR
    assert result.diagnostic == "http_status=302"


@pytest.mark.parametrize(
    ("response", "status"),
    [
        (TimeoutError(), RemoteCheckStatus.RELEASE_NOTES_TIMEOUT),
        (OSError("offline"), RemoteCheckStatus.RELEASE_NOTES_NETWORK_ERROR),
        (FakeResponse(status=404), RemoteCheckStatus.RELEASE_NOTES_HTTP_ERROR),
        (FakeResponse(b"x", headers={"Content-Length": "999999"}), RemoteCheckStatus.RELEASE_NOTES_TOO_LARGE),
        (FakeResponse(chunks=[b"x" * (256 * 1024 + 1)]), RemoteCheckStatus.RELEASE_NOTES_TOO_LARGE),
        (FakeResponse(b"\xff"), RemoteCheckStatus.RELEASE_NOTES_DECODING_ERROR),
    ],
)
def test_release_note_failures_preserve_available_update(response, status):
    transport = FakeTransport([FakeResponse(manifest_bytes()), response])
    result = check_for_updates("2.6.1", UpdatePreferences(), manifest_url="https://example.test/latest", release_notes_base_url="https://example.test/repo", transport=transport)
    assert result.status is status
    assert result.decision.reason is DecisionReason.UPDATE_AVAILABLE
    assert result.manifest is not None


def test_release_notes_base_requires_https_and_no_cross_origin_manifest_value_is_used():
    transport = FakeTransport([FakeResponse(manifest_bytes())])
    result = check_for_updates("2.6.1", UpdatePreferences(), manifest_url="https://example.test/latest", release_notes_base_url="http://attacker.test", transport=transport)
    assert result.status is RemoteCheckStatus.RELEASE_NOTES_URL_REJECTED
    assert len(transport.calls) == 1
