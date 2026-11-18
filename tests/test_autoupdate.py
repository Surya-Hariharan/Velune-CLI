"""Ask-first auto-update: gating, offers, accept/decline, spawning and `velune update` on Windows.

The index, ownership, uv lookup and Popen are all injected. Nothing is downloaded or run.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from velune.lifecycle import autoupdate as au
from velune.lifecycle.errors import ExitCode, LifecycleResult
from velune.lifecycle.ownership import OwnershipKind, OwnershipReport

NOW = 1_800_000_000.0


def _report(kind=OwnershipKind.UV_TOOL, **kw) -> OwnershipReport:
    return OwnershipReport(kind=kind, reasons=(), prefix=kw.pop("prefix", "/tools/velune-cli"),
                           version="0.9.9", **kw)  # fmt: skip


def _found(latest="1.0.0") -> LifecycleResult:
    return LifecycleResult(
        "update",
        exit_code=ExitCode.UPDATE_AVAILABLE,
        manual_command="uv tool upgrade velune-cli",
        result={"installed_version": "0.9.9", "latest_version": latest, "index": "pypi.org"},
    )


def _up_to_date() -> LifecycleResult:
    return LifecycleResult("update", result={"installed_version": "1.0.0"})


@pytest.fixture
def state(tmp_path):
    return tmp_path / "lifecycle" / "update_state.json"


@pytest.fixture(autouse=True)
def _uv_found(monkeypatch):
    monkeypatch.setattr("velune.lifecycle.uvpath.trusted_which", lambda name: "/usr/bin/uv")


# ── due() ─────────────────────────────────────────────────────────────────


def test_due_on_first_run():
    assert au.due("ask", 24, {}, now=NOW, environ={}, interactive=True)


@pytest.mark.parametrize(
    ("mode", "env", "interactive"),
    [
        ("off", {}, True),
        ("ask", {"CI": "true"}, True),
        ("ask", {"VELUNE_NO_UPDATE_CHECK": "1"}, True),
        ("ask", {}, False),
    ],
)
def test_not_due(mode, env, interactive):
    assert not au.due(mode, 24, {}, now=NOW, environ=env, interactive=interactive)


def test_due_respects_the_interval():
    recent = {"last_check": NOW - 2 * 3600}
    old = {"last_check": NOW - 25 * 3600}
    assert not au.due("ask", 24, recent, now=NOW, environ={}, interactive=True)
    assert au.due("ask", 24, old, now=NOW, environ={}, interactive=True)


# ── plan_for ──────────────────────────────────────────────────────────────


def test_uv_plan_waits_for_the_environment_and_repairs_with_extras():
    plan = au.plan_for(_report(extras=("rag",)), "1.0.0", which=lambda n: "uv")
    assert plan is not None
    assert plan.command == ["uv", "tool", "upgrade", "velune-cli"]
    assert plan.repair_command == ["uv", "tool", "install", "--force", "velune-cli[rag]==1.0.0"]
    assert plan.watch_files[-1] == plan.verify_command[0]  # the env's python.exe


@pytest.mark.parametrize(
    "report",
    [
        _report(specifier="==0.9.9"),
        _report(installed_from_local_file=True),
        _report(kind=OwnershipKind.EDITABLE),
        _report(kind=OwnershipKind.NOT_UV),
        _report(kind=OwnershipKind.AMBIGUOUS),
    ],
)
def test_manual_only_installations_get_no_plan(report):
    assert au.plan_for(report, "1.0.0", which=lambda n: "uv") is None


def test_no_uv_no_plan():
    assert au.plan_for(_report(), "1.0.0", which=lambda n: None) is None


def test_pip_user_plan_uses_the_running_interpreter_and_never_watches_it():
    plan = au.plan_for(_report(kind=OwnershipKind.PIP_USER), "1.0.0", executable="/py/python")
    assert plan is not None
    assert plan.command == ["/py/python", "-m", "pip", "install", "--upgrade", "--user",
                            "velune-cli"]  # fmt: skip
    assert plan.repair_command is None
    assert "/py/python" not in plan.watch_files  # the interpreter is shared with other programs


# ── check / accept / decline ──────────────────────────────────────────────


def test_check_offers_a_new_version_and_records_the_check(state):
    offer = au.check_for_update(run_check=_found, path=state, detect=_report)
    assert offer is not None and offer.latest == "1.0.0" and offer.plan is not None
    assert isinstance(json.loads(state.read_text())["last_check"], float)


def test_check_up_to_date_offers_nothing(state):
    assert au.check_for_update(run_check=_up_to_date, path=state, detect=_report) is None
    assert "last_check" in json.loads(state.read_text())


def test_declined_version_is_not_offered_again_but_the_next_one_is(state):
    offer = au.check_for_update(run_check=_found, path=state, detect=_report)
    au.decline(offer, state)
    assert au.check_for_update(run_check=_found, path=state, detect=_report) is None
    later = au.check_for_update(run_check=lambda: _found("1.0.1"), path=state, detect=_report)
    assert later is not None and later.latest == "1.0.1"


def test_accept_records_pending_and_suppresses_a_second_offer(state):
    offer = au.check_for_update(run_check=_found, path=state, detect=_report)
    assert au.accept(offer, state)
    pending = json.loads(state.read_text())["pending"]
    assert pending["target_version"] == "1.0.0"
    assert pending["command"][-2:] == ["upgrade", "velune-cli"]
    assert au.check_for_update(run_check=_found, path=state, detect=_report) is None


def test_manual_only_installation_is_offered_as_notice(state):
    detect = lambda: _report(kind=OwnershipKind.NOT_UV)  # noqa: E731
    offer = au.check_for_update(run_check=_found, path=state, detect=detect)
    assert offer is not None and offer.plan is None
    assert not au.accept(offer, state)


def test_check_never_raises(state):
    def boom():
        raise RuntimeError("network")

    assert au.check_for_update(run_check=boom, path=state, detect=_report) is None


def test_last_result_is_reported_once(state):
    au.save_state({"last_result": {"status": "updated", "message": "m"}}, state)
    assert au.take_last_result(state)["status"] == "updated"
    assert au.take_last_result(state) is None


# ── spawn_pending ─────────────────────────────────────────────────────────


def test_spawn_does_nothing_without_pending(state):
    calls = []
    assert not au.spawn_pending(path=state, popen=lambda *a, **k: calls.append(a))
    assert calls == []


def test_spawn_runs_a_copy_isolated_and_detached(state):
    offer = au.check_for_update(run_check=_found, path=state, detect=_report)
    au.accept(offer, state)
    seen = {}

    def popen(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs

    assert au.spawn_pending(path=state, parent_pid=4242, popen=popen)
    argv = seen["argv"]
    assert argv[1] == "-I"
    assert os.path.basename(argv[2]) == "post_exit_updater.py"
    spec = json.loads(argv[3])
    assert spec["parent_pid"] == 4242
    assert spec["target_version"] == "1.0.0"
    assert spec["state_path"] == str(state)
    kwargs = seen["kwargs"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    if os.name == "nt":
        assert kwargs["creationflags"] & 0x00000008  # DETACHED_PROCESS
    else:
        assert kwargs["start_new_session"] is True


def test_spawn_failure_is_silent(state):
    au.save_state({"pending": {"command": ["x"]}}, state)

    def popen(*a, **k):
        raise OSError("no")

    assert not au.spawn_pending(path=state, popen=popen)


# ── velune update on Windows ──────────────────────────────────────────────


def test_scheduled_update_spawns_and_says_so(state, monkeypatch):
    monkeypatch.setattr("velune.lifecycle.journal.default_journal_path",
                        lambda: state.parent / "journal.json")  # fmt: skip
    spawned = []
    result = au.run_scheduled_update(
        run_check=_found, detect=_report, spawn=lambda: spawned.append(1) or True, path=state
    )
    assert result.exit_code is ExitCode.OK
    assert result.result["scheduled"] is True
    assert spawned == [1]


def test_scheduled_update_up_to_date_does_nothing(state):
    spawned = []
    result = au.run_scheduled_update(
        run_check=_up_to_date, detect=_report, spawn=lambda: spawned.append(1), path=state
    )
    assert result.exit_code is ExitCode.OK and spawned == []


def test_scheduled_update_refuses_with_an_unfinished_journal(state, monkeypatch):
    journal = state.parent / "journal.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("{}")
    monkeypatch.setattr("velune.lifecycle.journal.default_journal_path", lambda: journal)
    spawned = []
    result = au.run_scheduled_update(
        run_check=_found, detect=_report, spawn=lambda: spawned.append(1), path=state
    )
    assert result.exit_code is not ExitCode.OK
    assert spawned == []


def test_scheduled_update_for_manual_install_returns_the_guidance(state, monkeypatch):
    monkeypatch.setattr("velune.lifecycle.journal.default_journal_path",
                        lambda: state.parent / "journal.json")  # fmt: skip
    result = au.run_scheduled_update(
        run_check=_found, detect=lambda: _report(kind=OwnershipKind.NOT_UV), path=state
    )
    assert result.exit_code is ExitCode.UPDATE_AVAILABLE
