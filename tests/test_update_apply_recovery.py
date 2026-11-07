"""P2.3.1: verification, interrupts and retained-journal guidance for `velune update`.

Same fake world as ``test_update_apply``: no real uv, no real upgrade.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.test_update_apply import UV, World, report
from velune.lifecycle import apply as apply_mod
from velune.lifecycle import journal as journal_mod
from velune.lifecycle.errors import ExitCode, LifecycleError
from velune.lifecycle.ownership import OwnershipKind

pytest.importorskip("packaging.version")


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def damage_after_uv(world, **fields):
    inner = world.on_uv

    def go():
        inner()
        for key, value in fields.items():
            setattr(world, key, value)

    world.on_uv = go


# ---- the version is not proof; the files must match what was installed -------------------------


@pytest.mark.parametrize(
    ("record", "problems", "expect"),
    [
        ("damaged", ("2 file(s) missing (e.g. velune/a.py, velune/b.py)",), "2 file(s) missing"),
        ("unverifiable", ("the package has no RECORD file",), "no RECORD file"),
    ],
)
def test_newer_version_with_unconfirmed_files_is_not_success(world, record, problems, expect):
    world.upgrade_to("0.9.10")
    damage_after_uv(world, record=record, problems=problems)
    result = world.go()
    assert result.exit_code is ExitCode.ERROR and result.result["applied"] is None
    assert "reads 0.9.10, but its files could not be confirmed" in result.message
    assert expect in result.message and "No automatic recovery" in result.message
    assert "rolled back" not in result.message.lower()
    assert len(world.calls) == 1 and world.journal.exists()  # no retry, nothing cleared
    assert result.manual_command == "uv tool install --force velune-cli"


def test_unchanged_version_with_damaged_files_keeps_the_journal(world):
    damage_after_uv(world, record="damaged", problems=("1 file(s) changed (e.g. velune/x.py)",))
    result = world.go()  # uv "does nothing": the version stays 0.9.9
    assert result.exit_code is ExitCode.ERROR and result.result["applied"] is None
    assert "still 0.9.9" in result.message and "does not show uv left" in result.message
    assert "1 file(s) changed" in result.message
    assert world.journal.exists() and len(world.calls) == 1


def test_unchanged_version_but_different_installed_packages_keeps_the_journal(world):
    damage_after_uv(world, dists=(("velune-cli", "0.9.9"), ("click", "9")))
    result = world.go()
    assert result.result["applied"] is None and world.journal.exists()
    assert "set of installed packages differs" in result.message


def test_unchanged_version_is_cleared_only_with_a_baseline_to_compare(world):
    world.integrity_raises_on = {1}  # the "before" snapshot failed
    result = world.go()
    assert result.result["applied"] is None and world.journal.exists()
    assert "could not be compared" in result.message


def test_integrity_check_crash_after_uv_is_unknown_and_redacted(world):
    world.integrity_raises_on = {2}
    result = world.go()
    assert result.result["applied"] is None and world.journal.exists()
    assert "integrity check could not run" in result.message
    assert "bob" not in json.dumps(result.to_dict())


def test_integrity_check_crash_before_uv_does_not_block_a_successful_update(world):
    world.upgrade_to("0.9.10")
    world.integrity_raises_on = {1}  # baseline unavailable; success does not need it
    result = world.go()
    assert result.exit_code is ExitCode.OK and result.result["applied"] is True


def test_verified_unchanged_needs_intact_files_and_the_same_packages(world):
    result = world.go()
    assert result.exit_code is ExitCode.ERROR and result.result["applied"] is False
    assert "still 0.9.9 (verified)" in result.message and not world.journal.exists()


def test_uv_nonzero_exit_without_change_is_cleared_only_when_verified(world):
    world.uv_result = SimpleNamespace(returncode=1, stdout="", stderr="")
    assert world.go().result["applied"] is False and not world.journal.exists()


def test_journal_contents_carry_no_paths_or_secrets(world):
    world.upgrade_to("0.9.10")
    seen: list[str] = []
    inner = world.on_uv
    world.on_uv = lambda: (seen.append(world.journal.read_text(encoding="utf-8")), inner())
    world.go()
    data = json.loads(seen[0])
    assert set(data) == {
        "operation",
        "phase",
        "started_at",
        "from_version",
        "specifier",
        "extras",
        "python_version",
        "python_base",
        "to_version",
        "installer",
        "schema_version",
    }
    assert str(world.journal.parent) not in seen[0] and "token" not in seen[0].lower()


# ---- interrupts around the post-update check -----------------------------------------------


def test_interrupt_during_the_post_check_is_retried_not_fatal(world):
    world.upgrade_to("0.9.10")
    calls = {"n": 0}

    def detect():
        calls["n"] += 1
        if calls["n"] == 2:  # the first call after uv finished
            raise KeyboardInterrupt
        return world.state

    result = world.go(detect=detect)
    assert result.exit_code is ExitCode.OK and result.result["applied"] is True
    assert any("deferred until uv finished" in w for w in result.warnings)
    assert not world.journal.exists()


def test_persistent_interrupts_in_the_post_check_keep_the_journal(world):
    world.upgrade_to("0.9.10")
    calls = {"n": 0}

    def detect():
        calls["n"] += 1
        if calls["n"] >= 2:
            raise KeyboardInterrupt
        return world.state

    result = world.go(detect=detect)
    assert result.exit_code is ExitCode.CANCELLED and result.result["applied"] is None
    assert world.journal.exists() and len(world.calls) == 1


# ---- a retained journal explains itself and offers only checked steps -----------------------


@pytest.mark.parametrize(
    ("phase", "wording"),
    [("planned", "stopped before uv was started"), ("applying", "uv may have run")],
)
def test_retained_journal_explains_the_block_and_gives_checked_steps(world, phase, wording):
    entry = journal_mod.new_entry("update", report(), python_version="3.12.4", to_version="0.9.10")
    journal_mod.write_journal(entry, world.journal)
    if phase == "applying":
        journal_mod.advance_phase(entry, "applying", world.journal)
    before = world.journal.read_bytes()

    result = world.go()
    data = result.to_dict()
    text = result.render_text()
    assert result.exit_code is ExitCode.REFUSED and world.calls == []
    assert data["result"]["reason"] == "journal_exists" and data["result"]["phase"] == phase
    assert data["result"]["from_version"] == "0.9.9" and data["result"]["to_version"] == "0.9.10"
    assert "will not start another one" in text and wording in text
    assert "uv tool list" in text and "velune --version" in text
    assert "Do not delete it before checking" in text and "journal.json" in text
    assert "velune doctor" not in text  # doctor does not inspect the journal
    assert world.journal.read_bytes() == before  # never touched


def test_unreadable_journal_is_described_not_trusted(world):
    world.journal.parent.mkdir(parents=True)
    world.journal.write_text("{ not json", encoding="utf-8")
    result = world.go()
    assert result.exit_code is ExitCode.REFUSED
    assert "could not be read" in result.message and "unknown" in result.message
    assert result.to_dict()["result"]["journal_state"] == "malformed"
    assert world.journal.read_text(encoding="utf-8") == "{ not json"


def test_write_journal_refusal_uses_the_same_guidance(tmp_path):
    path = tmp_path / "journal.json"
    path.write_text("earlier", encoding="utf-8")
    entry = journal_mod.new_entry("update", report(), python_version="3.12.4")
    with pytest.raises(LifecycleError) as caught:
        journal_mod.write_journal(entry, path)
    err = caught.value
    assert err.details["reason"] == "journal_exists" and "uv tool list" in (err.recovery or "")
    assert "velune doctor" not in (err.recovery or "") and path.read_text() == "earlier"


def test_unverified_outcome_points_at_the_journal_and_not_at_doctor(world):
    world.on_uv = lambda: setattr(world, "state", report(OwnershipKind.INDETERMINATE, version=None))
    result = world.go()
    text = result.render_text()
    assert "velune doctor" not in text and "journal" in text and "uv tool list" in text
    assert str(result.to_dict()["result"]["journal"]).endswith("journal.json")


# ---- cancelling never kills uv part-way ---------------------------------------------------


class FakePopen:
    script: list = []
    made: list = []

    def __init__(self, argv, **kw):
        self.argv, self.kw = argv, kw
        self.killed, self.returncode, self.waits = False, None, []
        self.steps = list(FakePopen.script)
        FakePopen.made.append(self)

    def wait(self, timeout=None):
        self.waits.append(timeout)
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        self.returncode = step
        return step

    def kill(self):
        self.killed = True


@pytest.fixture
def popen(monkeypatch):
    FakePopen.made = []
    monkeypatch.setattr(apply_mod.subprocess, "Popen", FakePopen)
    return FakePopen


def test_runner_starts_uv_with_an_argument_list_and_discards_its_output(popen):
    popen.script = [0]
    done = apply_mod._run_uv([UV, "tool", "upgrade", "velune-cli"], timeout=900.0)
    (proc,) = popen.made
    assert proc.argv == [UV, "tool", "upgrade", "velune-cli"] and done.returncode == 0
    assert proc.kw["stdin"] is subprocess.DEVNULL
    assert proc.kw["stdout"] is subprocess.DEVNULL and proc.kw["stderr"] is subprocess.DEVNULL
    assert proc.kw["start_new_session"] is (os.name == "posix")  # Ctrl-C does not reach uv
    assert "shell" not in proc.kw and proc.waits == [900.0]


def test_first_interrupt_waits_for_uv_instead_of_killing_it(popen, capsys):
    popen.script = [KeyboardInterrupt(), 0]
    with pytest.raises(KeyboardInterrupt):
        apply_mod._run_uv([UV], timeout=900.0)
    (proc,) = popen.made
    assert proc.killed is False and proc.waits == [900.0, apply_mod.CANCEL_GRACE]
    assert "Cancel requested" in capsys.readouterr().err


def test_second_interrupt_stops_uv(popen):
    popen.script = [KeyboardInterrupt(), KeyboardInterrupt(), 0]
    with pytest.raises(KeyboardInterrupt):
        apply_mod._run_uv([UV], timeout=900.0)
    assert popen.made[0].killed is True


def test_waiting_longer_than_the_grace_stops_uv(popen):
    popen.script = [KeyboardInterrupt(), subprocess.TimeoutExpired([UV], 1), 0]
    with pytest.raises(KeyboardInterrupt):
        apply_mod._run_uv([UV], timeout=900.0)
    assert popen.made[0].killed is True


def test_overall_timeout_stops_uv_and_reports_it(popen):
    popen.script = [subprocess.TimeoutExpired([UV], 900), 0]
    with pytest.raises(subprocess.TimeoutExpired):
        apply_mod._run_uv([UV], timeout=900.0)
    assert popen.made[0].killed is True


def test_runner_is_the_default_and_really_runs_a_process():
    assert inspect.signature(apply_mod.run_update).parameters["run"].default is apply_mod._run_uv
    done = apply_mod._run_uv([sys.executable, "-c", "raise SystemExit(3)"], timeout=60.0)
    assert done.returncode == 3 and done.stdout is None
    with pytest.raises(OSError):
        apply_mod._run_uv(["/definitely/not/a/real/uv"], timeout=5.0)
