import hashlib
import json
import stat

from hamchat.system_update_executor import SystemUpdateStatus, VerifiedStagedCandidate, install_system_files, install_verified_candidate, prepare_system_install, recover_system_install
from hamchat.updates import parse_release_manifest


def release():
    payload = b"release"; managed = b"managed"
    return parse_release_manifest({"schema_version": 2, "version": "2.7.0", "git_ref": "v2.7.0", "release_notes": "updates/2.7.0.md", "data_compatibility": {"database_schema_version": "2026-08-03.2", "data_layout_version": 1, "data_mutation_required": False}, "release_payload": {"url": "https://example.test/archive/v2.7.0.zip", "format": "zip", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "root_prefix": "HamChat-v2.7.0", "files": [{"path": "hamchat/a.py", "size": len(managed), "sha256": hashlib.sha256(managed).hexdigest()}], "removals": []}}).manifest


def test_system_only_executor_preserves_data_and_updates_managed_file(tmp_path):
    install, candidate, data, txn = (tmp_path / x for x in ("install", "candidate", "data", "txn"))
    (install / "hamchat").mkdir(parents=True); candidate.mkdir(); data.mkdir()
    (install / "hamchat/a.py").write_text("old"); (candidate / "hamchat").mkdir(); (candidate / "hamchat/a.py").write_text("new")
    sentinel = data / "sentinel"; sentinel.write_bytes(b"user")
    result = install_system_files(manifest=release(), candidate_root=candidate, installation_root=install, data_root=data, managed_paths=("hamchat/a.py",), transaction_root=txn)
    assert result.status is SystemUpdateStatus.INSTALLED
    assert (install / "hamchat/a.py").read_text() == "new" and sentinel.read_bytes() == b"user"
    assert not result.source_mutation_permitted and not result.user_data_mutation_permitted


def test_data_payload_is_blocked(tmp_path):
    root = tmp_path / "r"; root.mkdir(); data = tmp_path / "data"; data.mkdir()
    result = install_system_files(manifest=release(), candidate_root=root, installation_root=root, data_root=data, managed_paths=("data/x",), transaction_root=tmp_path / "t")
    assert result.status is SystemUpdateStatus.BLOCKED


def test_verified_staging_binds_transaction_and_digest(tmp_path):
    install, staged, data, txn = (tmp_path / x for x in ("install", "staged", "data", "tx-0000001"))
    install.mkdir(); staged.mkdir(); data.mkdir(); (staged / "hamchat").mkdir()
    payload = b"new"; (staged / "hamchat/a.py").write_bytes(payload)
    candidate = VerifiedStagedCandidate("tx-0000001", staged, release(), (("hamchat/a.py", hashlib.sha256(payload).hexdigest()),), True)
    prepare_system_install(candidate, txn)
    assert install_verified_candidate(candidate=candidate, installation_root=install, data_root=data, transaction_root=txn).status is SystemUpdateStatus.INSTALLED
    (staged / "hamchat/a.py").write_bytes(b"changed")
    assert install_verified_candidate(candidate=candidate, installation_root=install, data_root=data, transaction_root=txn).status is SystemUpdateStatus.BLOCKED
    assert recover_system_install(candidate=candidate, transaction_root=txn).status is SystemUpdateStatus.INSTALLED


def test_verified_install_preserves_existing_executable_mode(tmp_path):
    install, staged, data, txn = (tmp_path / value for value in ("install", "staged", "data", "txn"))
    install.mkdir(); staged.mkdir(); data.mkdir()
    target = install / "run_hamchat.sh"; target.write_bytes(b"old"); target.chmod(0o755)
    source = staged / "run_hamchat.sh"; source.write_bytes(b"new"); source.chmod(0o644)
    candidate = VerifiedStagedCandidate("txn", staged, release(), (("run_hamchat.sh", hashlib.sha256(b"new").hexdigest()),), True)
    prepare_system_install(candidate, txn)
    assert install_verified_candidate(candidate=candidate, installation_root=install, data_root=data, transaction_root=txn).status is SystemUpdateStatus.INSTALLED
    assert target.read_bytes() == b"new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_verified_install_preserves_existing_non_executable_mode(tmp_path):
    install, staged, data, txn = (tmp_path / value for value in ("install", "staged", "data", "txn"))
    install.mkdir(); staged.mkdir(); data.mkdir()
    target = install / "managed.txt"; target.write_bytes(b"old"); target.chmod(0o640)
    source = staged / "managed.txt"; source.write_bytes(b"new")
    candidate = VerifiedStagedCandidate("txn", staged, release(), (("managed.txt", hashlib.sha256(b"new").hexdigest()),), True)
    prepare_system_install(candidate, txn)
    assert install_verified_candidate(candidate=candidate, installation_root=install, data_root=data, transaction_root=txn).status is SystemUpdateStatus.INSTALLED
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_legacy_install_preserves_existing_mode(tmp_path):
    install, candidate, data, txn = (tmp_path / value for value in ("install", "candidate", "data", "txn"))
    install.mkdir(); candidate.mkdir(); data.mkdir()
    target = install / "hamchat" / "a.py"; target.parent.mkdir(); target.write_bytes(b"old"); target.chmod(0o640)
    source = candidate / "hamchat" / "a.py"; source.parent.mkdir(); source.write_bytes(b"new"); source.chmod(0o755)
    assert install_system_files(manifest=release(), candidate_root=candidate, installation_root=install, data_root=data, managed_paths=("hamchat/a.py",), transaction_root=txn).status is SystemUpdateStatus.INSTALLED
    assert target.read_bytes() == b"new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_rollback_restores_original_bytes_and_mode(tmp_path):
    install, staged, data, txn = (tmp_path / value for value in ("install", "staged", "data", "txn"))
    install.mkdir(); staged.mkdir(); data.mkdir()
    target = install / "run_hamchat.sh"; target.write_bytes(b"old"); target.chmod(0o755)
    source = staged / "run_hamchat.sh"; source.write_bytes(b"new")
    candidate = VerifiedStagedCandidate("txn", staged, release(), (("run_hamchat.sh", hashlib.sha256(b"new").hexdigest()),), True)
    prepare_system_install(candidate, txn)
    journal = json.loads((txn / "system-install.json").read_text())
    backup = txn / "rollback" / "run_hamchat.sh"; backup.parent.mkdir()
    backup.write_bytes(b"old"); backup.chmod(0o755)
    journal["files"][0].update(old=hashlib.sha256(b"old").hexdigest(), old_mode=0o755, rollback="rollback/run_hamchat.sh")
    journal["state"] = "system_install_started"
    (txn / "system-install.json").write_text(json.dumps(journal))
    target.write_bytes(b"new"); target.chmod(0o644)
    assert recover_system_install(candidate=candidate, transaction_root=txn, installation_root=install).status is SystemUpdateStatus.ROLLED_BACK
    assert target.read_bytes() == b"old"
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
