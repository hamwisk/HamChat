from __future__ import annotations

import json
import pytest

from hamchat.settings import load_settings
from hamchat.updates import (
    DecisionReason,
    UpdateMode,
    UpdatePreferences,
    UpdateValidationError,
    decide_update,
    parse_release_manifest,
    preferences_from_settings,
    save_update_preferences,
)


def manifest(version="2.7.0", **changes):
    value = {
        "schema_version": 1,
        "version": version,
        "git_ref": f"v{version}",
        "release_notes": "updates/2.7.0.md",
        "minimum_updater_version": "2.7.0",
    }
    value.update(changes)
    return value


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
    assert decide_update(installed, manifest(remote), UpdatePreferences(), updater_version="2.7.0").reason is reason


@pytest.mark.parametrize(("remote", "reason"), [("2.6.2", DecisionReason.PATCH_UPDATE_IGNORED), ("2.7.0", DecisionReason.UPDATE_AVAILABLE), ("3.0.0", DecisionReason.UPDATE_AVAILABLE)])
def test_patch_filter(remote, reason):
    assert decide_update("2.6.1", manifest(remote), UpdatePreferences(ignore_patch_updates=True), updater_version="2.7.0").reason is reason


@pytest.mark.parametrize(("mode", "manual", "reason"), [(UpdateMode.AUTOMATIC, False, DecisionReason.UPDATE_AVAILABLE), (UpdateMode.PROMPT, False, DecisionReason.UPDATE_AVAILABLE), (UpdateMode.OFF, False, DecisionReason.UPDATE_MODE_OFF), (UpdateMode.OFF, True, DecisionReason.UPDATE_AVAILABLE)])
def test_modes(mode, manual, reason):
    assert decide_update("2.6.1", manifest(), UpdatePreferences(mode=mode), manual_check=manual, updater_version="2.7.0").reason is reason


def test_skips_are_exact_and_manual_bypasses_only_the_skip():
    preferences = UpdatePreferences(skipped_version="2.7.0")
    assert decide_update("2.6.1", manifest(), preferences, updater_version="2.7.0").reason is DecisionReason.VERSION_SKIPPED
    assert decide_update("2.6.1", manifest("2.7.1"), preferences, updater_version="2.7.0").reason is DecisionReason.UPDATE_AVAILABLE
    assert decide_update("2.6.1", manifest(), preferences, manual_check=True, updater_version="2.7.0").reason is DecisionReason.UPDATE_AVAILABLE
    assert decide_update("2.7.1", manifest(), preferences, manual_check=True, updater_version="2.7.0").reason is DecisionReason.REMOTE_NOT_NEWER


@pytest.mark.parametrize("installed", ["2.7", "v2.7.0", 270, True])
def test_invalid_installed_versions_are_controlled(installed):
    assert decide_update(installed, manifest(), UpdatePreferences(), updater_version="2.7.0").reason is DecisionReason.INVALID_INSTALLED_VERSION


@pytest.mark.parametrize("remote", ["2.7", "2.7.0-dev", "2.7.0-alpha.1", True])
def test_invalid_or_prerelease_remote_versions_are_rejected(remote):
    assert parse_release_manifest(manifest(remote)).reason is DecisionReason.INVALID_MANIFEST


@pytest.mark.parametrize("change", [{"schema_version": None}, {"schema_version": True}, {"version": 2}, {"git_ref": ""}, {"release_notes": "../secret.md"}, {"release_notes": "/updates/2.7.0.md"}, {"minimum_updater_version": "2.7"}])
def test_manifest_field_validation(change):
    assert parse_release_manifest(manifest(**change)).reason is DecisionReason.INVALID_MANIFEST


def test_manifest_structure_and_future_schema_are_controlled():
    incomplete = manifest(); del incomplete["git_ref"]
    assert parse_release_manifest(incomplete).reason is DecisionReason.INVALID_MANIFEST
    assert parse_release_manifest(manifest(extra="no")).reason is DecisionReason.INVALID_MANIFEST
    assert parse_release_manifest(manifest(schema_version=2)).reason is DecisionReason.UNSUPPORTED_MANIFEST_SCHEMA


@pytest.mark.parametrize("raw", [{}, {"mode": "bad", "ignore_patch_updates": False, "skipped_version": None}, {"mode": "prompt", "ignore_patch_updates": 1, "skipped_version": None}, {"mode": "prompt", "ignore_patch_updates": False, "skipped_version": "2.7.0-dev"}])
def test_invalid_preferences_are_controlled(raw):
    assert decide_update("2.6.1", manifest(), raw, updater_version="2.7.0").reason is DecisionReason.INVALID_UPDATE_PREFERENCES


def test_minimum_updater_is_an_independent_required_capability():
    assert decide_update("2.6.1", manifest(minimum_updater_version="2.7.1"), UpdatePreferences(), updater_version="2.7.0").reason is DecisionReason.UPDATER_INCOMPATIBLE
    assert decide_update("2.6.1", manifest(), UpdatePreferences()).reason is DecisionReason.UPDATER_INCOMPATIBLE
    assert decide_update("2.6.1", manifest(), UpdatePreferences(), updater_version="2.7.0").reason is DecisionReason.UPDATE_AVAILABLE


def test_settings_defaults_and_persistence(tmp_path):
    path = tmp_path / "app.json"
    settings = load_settings(path)
    assert preferences_from_settings(settings) == UpdatePreferences()
    save_update_preferences(path, settings, UpdatePreferences(mode=UpdateMode.AUTOMATIC, skipped_version="2.7.0"))
    assert json.loads(path.read_text())["updates"]["mode"] == "automatic"


def test_preference_model_requires_typed_mode():
    with pytest.raises(UpdateValidationError):
        UpdatePreferences(mode="prompt")
