"""The post-exit updater: waits until Velune's executables are free, then updates and verifies.

Commands and the clock are injected; nothing is installed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from velune.lifecycle import post_exit_updater as peu

# ── waiting ───────────────────────────────────────────────────────────────


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def test_waits_while_velune_is_in_use(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(peu, "in_use", lambda spec: clock.t < 5)
    assert peu.wait_until_clear({"wait_timeout": 60}, sleep=clock.sleep, clock=clock)
    assert clock.t >= 5


def test_gives_up_when_velune_stays_open(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(peu, "in_use", lambda spec: True)
    assert not peu.wait_until_clear({"wait_timeout": 10}, sleep=clock.sleep, clock=clock)


def test_free_and_missing_files_do_not_block(tmp_path):
    free = tmp_path / "velune.exe"
    free.write_bytes(b"x")
    spec = {"watch_files": [str(free), str(tmp_path / "missing.exe")]}
    assert not peu.in_use(spec)
    assert free.read_bytes() == b"x", "the probe must not change the file"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows locks running executables")
def test_a_running_executable_blocks(tmp_path):
    exe = tmp_path / "velune.exe"
    base = getattr(sys, "_base_executable", sys.executable)
    exe.write_bytes(Path(base).read_bytes())
    proc = subprocess.Popen([str(exe), "-c", "import time; time.sleep(5)"],
                            cwd=os.path.dirname(base))  # fmt: skip
    try:
        assert peu.in_use({"watch_files": [str(exe)]})
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX waits for the parent process")
def test_posix_waits_for_the_parent():
    assert peu.in_use({"parent_pid": os.getpid()})
    assert not peu.in_use({"parent_pid": None})


# ── apply_update ──────────────────────────────────────────────────────────


def _spec(**extra):
    return {
        "command": ["uv", "tool", "upgrade", "velune-cli"],
        "repair_command": ["uv", "tool", "install", "--force", "velune-cli==1.0.0"],
        "verify_command": ["python", "-m", "velune", "--version"],
        "target_version": "1.0.0",
        "from_version": "0.9.9",
        "manual_command": "uv tool upgrade velune-cli",
    } | extra


class _Runner:
    def __init__(self, versions: list[str]) -> None:
        self.calls: list[list[str]] = []
        self.versions = list(versions)

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        if argv[-1] == "--version":
            return 0, f"velune v{self.versions.pop(0)}\n"
        return 0, ""


def test_verified_update_needs_no_repair():
    run = _Runner(["1.0.0"])
    assert peu.apply_update(_spec(), run=run)["status"] == "updated"
    assert len(run.calls) == 2


def test_unverified_update_is_repaired_once():
    run = _Runner(["0.9.9", "1.0.0"])
    result = peu.apply_update(_spec(), run=run)
    assert result["status"] == "updated" and "repair" in result["message"]
    assert sum(1 for c in run.calls if "install" in c) == 1


def test_failure_after_repair_reports_the_manual_command():
    result = peu.apply_update(_spec(), run=_Runner(["0.9.9", "0.9.9"]))
    assert result["status"] == "failed"
    assert result["manual_command"] == "uv tool upgrade velune-cli"


def test_pip_installs_have_no_repair_step():
    run = _Runner(["0.9.9"])
    assert peu.apply_update(_spec(repair_command=None), run=run)["status"] == "failed"
    assert len(run.calls) == 2


def test_version_must_match_exactly():
    assert not peu.verified(_spec(), _Runner(["1.0.0.dev1"]))


# ── lock and main ─────────────────────────────────────────────────────────


def test_lock_is_exclusive_until_stale(tmp_path):
    lock = str(tmp_path / "l.lock")
    assert peu.acquire_lock(lock)
    assert not peu.acquire_lock(lock)
    later = os.path.getmtime(lock) + peu.STALE_LOCK_S + 1
    assert peu.acquire_lock(lock, now=lambda: later)


def _main_spec(tmp_path, state):
    return json.dumps(
        _spec(
            state_path=str(state),
            lock_path=str(tmp_path / "l.lock"),
            log_path=str(tmp_path / "update.log"),
        )  # fmt: skip
    )


def test_main_records_the_result_and_clears_pending(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"pending": {"target_version": "1.0.0"}, "keep": 1}))
    monkeypatch.setattr(peu, "wait_until_clear", lambda s: True)
    monkeypatch.setattr(peu, "apply_update",
                        lambda s, log: peu._result("updated", s, "ok"))  # fmt: skip
    assert peu.main([_main_spec(tmp_path, state)]) == 0
    data = json.loads(state.read_text())
    assert data["pending"] is None and data["keep"] == 1
    assert data["last_result"]["status"] == "updated"
    assert not (tmp_path / "l.lock").exists()


def test_main_keeps_pending_when_velune_never_exits(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"pending": {"target_version": "1.0.0"}}))
    monkeypatch.setattr(peu, "wait_until_clear", lambda s: False)
    assert peu.main([_main_spec(tmp_path, state)]) == 0
    assert json.loads(state.read_text())["pending"] == {"target_version": "1.0.0"}


def test_main_rejects_a_bad_spec():
    assert peu.main(["not json"]) == 2
    assert peu.main([json.dumps({"command": ["x"]})]) == 2


def test_helper_is_stdlib_only_and_runs_isolated(tmp_path):
    done = subprocess.run(
        [sys.executable, "-I", peu.__file__], capture_output=True, text=True, timeout=60,
        cwd=tmp_path, check=False,
    )  # fmt: skip
    assert done.returncode == 2, done.stderr  # usage error, not an ImportError
    assert "Traceback" not in done.stderr
