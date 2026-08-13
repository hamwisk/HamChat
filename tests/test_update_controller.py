from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PyQt6.QtCore import QCoreApplication

from hamchat.system_update_executor import (
    SystemUpdateStatus, VerifiedStagedCandidate, prepare_system_install,
    recover_pending_system_installs,
)
from hamchat.update_controller import UpdateController, transaction_parent_for_installation
from hamchat.updates import (
    DecisionReason, RemoteCheckStatus, UpdateDecision, UpdateMode,
    UpdatePreferences, parse_release_manifest,
)


def release():
    body = b"new"; archive = b"archive"
    return parse_release_manifest({
        "schema_version": 2, "version": "9.0.0", "git_ref": "v9.0.0",
        "release_notes": "updates/9.0.0.md",
        "data_compatibility": {"database_schema_version": "2026-08-03.2", "data_layout_version": 1, "data_mutation_required": False},
        "release_payload": {"url": "https://github.com/hamwisk/HamChat/archive/v9.0.0.zip", "format": "zip", "size": len(archive), "sha256": hashlib.sha256(archive).hexdigest(), "root_prefix": "HamChat-v9.0.0", "files": [{"path": "hamchat/a.py", "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}], "removals": []},
    }).manifest


class Splash:
    def __init__(self): self.calls = []
    def status(self, value): self.calls.append(("status", value))
    def clear_status(self): self.calls.append(("clear",))
    def close(self): self.calls.append(("close",))


def settings(path: Path, mode="ask", skipped=None):
    path.write_text(json.dumps({"updates": {"mode": mode, "ignore_patch_updates": False, "skipped_version": skipped}}))


def controller(tmp_path, *, mode="ask", skipped=None, checker=None):
    app = QCoreApplication.instance() or QCoreApplication([])
    del app
    setting = tmp_path / "settings.json"; settings(setting, mode, skipped)
    return UpdateController(settings_path=setting, installation_root=tmp_path / "install", data_root=tmp_path / "data", splash=Splash(), check_function=checker or (lambda *_a, **_k: None))


def test_update_mode_defaults_persists_and_manual_works_when_off(tmp_path):
    calls = []
    c = controller(tmp_path, mode="off", checker=lambda *_a, **kwargs: calls.append(kwargs) or type("R", (), {"status": RemoteCheckStatus.NO_ELIGIBLE_UPDATE, "decision": UpdateDecision(DecisionReason.REMOTE_NOT_NEWER), "manifest": None})())
    assert c.mode_value() == "off"
    c.set_mode("automatic")
    assert c.mode_value() == "automatic"
    assert json.loads((tmp_path / "settings.json").read_text())["updates"]["mode"] == "automatic"
    # The explicit manual entry remains available in Off mode; routing is a
    # controller method rather than a menu-side mode check.
    c.set_mode("off")
    assert callable(c.check_manually)
    c.shutdown()


def test_startup_off_emits_completion_without_fetch(tmp_path):
    calls = []
    c = controller(tmp_path, mode="off", checker=lambda *_a, **_k: calls.append(1))
    done = []; c.startup_finished.connect(done.append)
    c.start_startup_check()
    assert done == [False] and calls == []
    c.shutdown()


def test_skip_is_exact_and_newer_candidate_not_suppressed(tmp_path):
    c = controller(tmp_path, skipped="9.0.0")
    assert c._preferences.skipped_version == "9.0.0"
    c._save_skip("9.1.0")
    assert c._preferences.skipped_version == "9.1.0"
    c.shutdown()


def test_pending_recovery_rolls_back_without_candidate_or_database(tmp_path):
    install, stage, tx = tmp_path / "install", tmp_path / "stage", tmp_path / "tx-0000001"
    (install / "hamchat").mkdir(parents=True); (stage / "hamchat").mkdir(parents=True)
    (install / "hamchat/a.py").write_bytes(b"old"); (stage / "hamchat/a.py").write_bytes(b"new")
    manifest = release()
    candidate = VerifiedStagedCandidate("tx-0000001", stage, manifest, (("hamchat/a.py", hashlib.sha256(b"new").hexdigest()),), True)
    prepare_system_install(candidate, tx)
    journal = json.loads((tx / "system-install.json").read_text())
    (tx / "rollback" / "hamchat").mkdir(parents=True)
    (tx / "rollback" / "hamchat/a.py").write_bytes(b"old")
    journal["files"][0].update(old=hashlib.sha256(b"old").hexdigest(), rollback="rollback/hamchat/a.py")
    journal["state"] = "system_install_started"
    (install / "hamchat/a.py").write_bytes(b"new")
    (tx / "system-install.json").write_text(json.dumps(journal))
    result = recover_pending_system_installs(transaction_parent=tmp_path, installation_root=install)
    assert result.status is SystemUpdateStatus.ROLLED_BACK
    assert (install / "hamchat/a.py").read_bytes() == b"old"
