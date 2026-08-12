from __future__ import annotations

import json

from hamchat.infra.llm.ollama_registry import _apply_context_overrides, _infer_vision
from hamchat.infra.llm.registry_layers import (
    CONTEXT_FILE, CONTEXT_USER_FILE, MODALITY_FILE, MODALITY_USER_FILE,
    RegistryDiagnosticCode, load_effective_registries,
)


def context():
    return {"version": 1, "by_name": {"model:latest": 8192}, "by_regex": {"vision.*": 4096}, "default_by_family": {"family": 2048}}


def modality():
    return {"version": 1, "defaults_to": "text", "families_multimodal": ["vision-family"], "name_contains_multimodal": ["vision"], "regex_multimodal": ["vision-.*"], "model_overrides": {"model:latest": "multimodal"}}


def context_user(**changes):
    result = {"version": 1, "by_name": {}, "by_regex": {}, "default_by_family": {}, "remove_by_name": [], "remove_by_regex": [], "remove_default_by_family": []}
    result.update(changes); return result


def modality_user(**changes):
    result = {"version": 1, "add": {}, "remove": {}, "model_overrides": {}, "remove_model_overrides": []}
    result.update(changes); return result


def write(path, name, value):
    path.mkdir(exist_ok=True)
    (path / name).write_text(json.dumps(value), encoding="utf-8")


def test_valid_layers_merge_user_values_and_tombstones_without_writing(tmp_path):
    write(tmp_path, CONTEXT_FILE, context()); write(tmp_path, MODALITY_FILE, modality())
    write(tmp_path, CONTEXT_USER_FILE, context_user(by_name={"MODEL:LATEST": 16384, "new": 4096}, remove_by_regex=["vision.*"]))
    write(tmp_path, MODALITY_USER_FILE, modality_user(add={"families_multimodal": ["new", "vision-family"], "name_contains_multimodal": ["custom"], "regex_multimodal": ["custom.*"]}, remove={"name_contains_multimodal": ["vision"]}, model_overrides={"MODEL:LATEST": "text"}, remove_model_overrides=["future:model"]))
    before = {item.name: item.read_bytes() for item in tmp_path.iterdir()}
    result = load_effective_registries(tmp_path)
    assert result.context.by_name == {"model:latest": 16384, "new": 4096}
    assert result.context.by_regex == {}
    assert result.modality.trigger_lists["families_multimodal"] == ("vision-family", "new")
    assert result.modality.trigger_lists["name_contains_multimodal"] == ("custom",)
    assert result.modality.model_overrides["model:latest"] == "text"
    assert before == {item.name: item.read_bytes() for item in tmp_path.iterdir()}


def test_missing_user_is_normal_and_malformed_user_is_ignored_untouched(tmp_path):
    write(tmp_path, CONTEXT_FILE, context()); write(tmp_path, MODALITY_FILE, modality())
    assert not load_effective_registries(tmp_path).diagnostics
    user = tmp_path / CONTEXT_USER_FILE; user.write_text("{broken", encoding="utf-8")
    original = user.read_bytes()
    result = load_effective_registries(tmp_path)
    assert result.context.by_name["model:latest"] == 8192
    assert result.diagnostics == (result.diagnostics[0],)
    assert result.diagnostics[0].code is RegistryDiagnosticCode.MALFORMED_JSON
    assert user.read_bytes() == original


def test_invalid_complete_layers_report_controlled_diagnostics(tmp_path):
    write(tmp_path, CONTEXT_FILE, {"version": 2})
    write(tmp_path, MODALITY_FILE, {**modality(), "regex_multimodal": ["["]})
    result = load_effective_registries(tmp_path)
    assert {item.code for item in result.diagnostics} == {RegistryDiagnosticCode.UNSUPPORTED_VERSION, RegistryDiagnosticCode.INVALID_REGEX}
    assert result.context.by_name == {} and result.modality.defaults_to == "text"


def test_contradictions_and_defaults_to_user_override_reject_whole_user_layer(tmp_path):
    write(tmp_path, CONTEXT_FILE, context()); write(tmp_path, MODALITY_FILE, modality())
    write(tmp_path, CONTEXT_USER_FILE, context_user(by_name={"model:latest": 1024}, remove_by_name=["model:latest"]))
    write(tmp_path, MODALITY_USER_FILE, {**modality_user(), "defaults_to": "multimodal"})
    result = load_effective_registries(tmp_path)
    assert result.context.by_name["model:latest"] == 8192
    assert result.modality.defaults_to == "text"
    assert [item.code for item in result.diagnostics] == [RegistryDiagnosticCode.CONTRADICTORY_OPERATION, RegistryDiagnosticCode.UNEXPECTED_FIELD]


def test_consumer_uses_effective_layers(tmp_path, monkeypatch):
    settings = tmp_path / "settings"
    write(settings, CONTEXT_FILE, context()); write(settings, MODALITY_FILE, modality())
    write(settings, CONTEXT_USER_FILE, context_user(remove_by_name=["model:latest"], by_name={"custom:latest": 32768}))
    write(settings, MODALITY_USER_FILE, modality_user(add={"name_contains_multimodal": ["customvision"]}, remove={"name_contains_multimodal": ["vision"]}))
    monkeypatch.chdir(tmp_path)
    entry = {"name": "custom:latest", "family": None, "context": None}
    _apply_context_overrides(entry)
    assert entry["context"] == 32768
    triggers = __import__("hamchat.infra.llm.ollama_registry", fromlist=["_load_triggers"])._load_triggers()
    assert _infer_vision("customvision:latest", None, {}, triggers)
    assert not _infer_vision("vision:latest", None, {}, triggers)
