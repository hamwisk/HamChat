from __future__ import annotations

import errno
import stat
import sys
from pathlib import Path
from unittest.mock import patch

from hamchat.app import _ensure_flushable_standard_streams, repair_known_launcher_modes
from hamchat.update_controller import UpdateController


class BrokenStream:
    closed = False

    def flush(self):
        raise OSError(errno.EIO, "Input/output error")


def test_repair_known_launchers_only_for_regular_files(tmp_path):
    run = tmp_path / "run_hamchat.sh"; setup = tmp_path / "setup_venv.sh"; other = tmp_path / "other.sh"
    for path in (run, setup, other):
        path.write_text("#!/bin/sh\n")
        path.chmod(0o644)
    repair_known_launcher_modes(tmp_path)
    assert stat.S_IMODE(run.stat().st_mode) == 0o755
    assert stat.S_IMODE(setup.stat().st_mode) == 0o755
    assert stat.S_IMODE(other.stat().st_mode) == 0o644


def test_repair_ignores_symlink_launcher(tmp_path):
    target = tmp_path / "target"; target.write_text("x"); target.chmod(0o644)
    (tmp_path / "run_hamchat.sh").symlink_to(target)
    repair_known_launcher_modes(tmp_path)
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_broken_standard_stream_is_replaced_but_healthy_one_is_untouched(monkeypatch):
    class HealthyStream:
        closed = False
        def flush(self): pass
    broken = BrokenStream()
    healthy = HealthyStream()
    monkeypatch.setattr(sys, "stdout", broken)
    monkeypatch.setattr(sys, "stderr", healthy)
    _ensure_flushable_standard_streams()
    assert sys.stdout is not broken
    assert sys.stderr is healthy


def test_healthy_terminal_streams_remain_untouched(monkeypatch):
    class HealthyStream:
        closed = False
        def flush(self): pass
    stdout, stderr = HealthyStream(), HealthyStream()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    _ensure_flushable_standard_streams()
    assert sys.stdout is stdout and sys.stderr is stderr


def test_replacement_launch_detaches_all_standard_streams(tmp_path):
    controller = type("Controller", (), {"_installation_root": Path(tmp_path)})()
    with patch("hamchat.update_controller.subprocess.Popen") as popen:
        assert UpdateController._launch_replacement(controller)
    _, kwargs = popen.call_args
    import subprocess
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["close_fds"] is True
    assert kwargs["start_new_session"] is True
