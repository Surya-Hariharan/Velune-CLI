"""Lifecycle error/result model and the Windows self-mutation guard (P2.1).

No test here starts uv or changes an installation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from velune.lifecycle.errors import (
    Category,
    ExitCode,
    LifecycleError,
    LifecycleResult,
    category_for,
)
from velune.lifecycle.ownership import OwnershipKind, OwnershipReport
from velune.lifecycle.safety import check_not_self_mutation, manual_command

DESIGN_EXIT_CODES = {
    "OK": 0,
    "ERROR": 1,
    "USAGE": 2,
    "REFUSED": 3,
    "NETWORK": 4,
    "VALIDATION_FAILED": 5,
    "APPLY_FAILED_ROLLED_BACK": 6,
    "ROLLBACK_FAILED": 7,
    "BUSY": 8,
    "UPDATE_AVAILABLE": 10,
    "CANCELLED": 130,
}


def uv_report(**overrides) -> OwnershipReport:
    base: dict = {
        "kind": OwnershipKind.UV_TOOL,
        "reasons": ("ok",),
        "prefix": "C:/tools/velune-cli",
        "version": "0.9.9",
        "specifier": None,
        "extras": (),
    }
    return OwnershipReport(**(base | overrides))


# ---- exit codes -----------------------------------------------------------


def test_exit_codes_match_the_design_table_exactly():
    assert {c.name: int(c) for c in ExitCode} == DESIGN_EXIT_CODES


def test_every_exit_code_has_one_distinct_category():
    categories = [category_for(c) for c in ExitCode]
    assert len(set(categories)) == len(list(ExitCode)) == len(list(Category))


@pytest.mark.parametrize("code", list(ExitCode))
def test_result_status_and_exit_code_agree(code):
    result = LifecycleResult("update", exit_code=code)
    assert result.to_dict()["exit_code"] == int(code)
    assert result.to_dict()["status"] == category_for(code).value


# ---- results ---------------------------------------------------------------


def test_json_shape_follows_the_design():
    result = LifecycleResult(
        "update",
        message="done",
        installation={"kind": "uv_tool", "owned": True, "path": "/p"},
        plan=("step one",),
        result={"from": "0.9.8", "to": "0.9.9"},
        warnings=("heads up",),
    )
    data = result.to_dict()
    assert set(data) >= {
        "command",
        "status",
        "exit_code",
        "installation",
        "plan",
        "result",
        "warnings",
    }
    assert data["status"] == "ok"
    json.dumps(data)  # must serialise


def test_error_result_carries_recovery_and_manual_command():
    err = LifecycleError(
        ExitCode.REFUSED, "no", recovery="do this", manual_command="uv tool upgrade velune-cli"
    )
    data = LifecycleResult.from_error("update", err).to_dict()
    assert data["exit_code"] == 3 and data["status"] == "refused"
    assert data["recovery"] == {
        "guidance": "do this",
        "manual_command": "uv tool upgrade velune-cli",
    }


def test_text_and_json_say_the_same_thing():
    err = LifecycleError(ExitCode.REFUSED, "nope", recovery="try this", manual_command="cmd x")
    result = LifecycleResult.from_error("uninstall", err)
    text = result.render_text()
    data = result.to_dict()
    assert data["message"] in text
    assert data["recovery"]["guidance"] in text
    assert f"Run: {data['recovery']['manual_command']}" in text


def test_update_available_and_ok_count_as_success():
    assert LifecycleResult("update", exit_code=ExitCode.UPDATE_AVAILABLE).ok
    assert LifecycleResult("update", exit_code=ExitCode.OK).ok
    assert not LifecycleResult("update", exit_code=ExitCode.REFUSED).ok


def test_unexpected_exception_does_not_leak_its_text():
    secret = "sk-ant-api03-" + "A" * 40
    result = LifecycleResult.from_exception("repair", RuntimeError(f"boom {secret} C:/Users/bob/x"))
    data = result.to_dict()
    assert data["exit_code"] == int(ExitCode.ERROR)
    assert "RuntimeError" in data["message"]
    assert secret not in json.dumps(data) and "bob" not in json.dumps(data)
    assert "velune doctor" in data["recovery"]["guidance"]


def test_keyboard_interrupt_maps_to_cancelled():
    result = LifecycleResult.from_exception("update", KeyboardInterrupt())
    assert result.exit_code is ExitCode.CANCELLED and result.exit_code == 130


def test_lifecycle_error_passes_through_from_exception():
    err = LifecycleError(ExitCode.NETWORK, "offline")
    assert LifecycleResult.from_exception("update", err).exit_code is ExitCode.NETWORK


def test_output_scrubs_home_paths_and_secrets():
    home = str(Path.home())
    err = LifecycleError(
        ExitCode.REFUSED,
        f"failed in {home}\\x token sk-ant-api03-{'B' * 40}",
        recovery=f"look at {home}",
    )
    blob = json.dumps(LifecycleResult.from_error("update", err).to_dict())
    assert home not in blob
    assert "B" * 40 not in blob


# ---- Windows self-mutation guard ------------------------------------------


@pytest.mark.parametrize("operation", ["update", "repair", "uninstall"])
def test_windows_refuses_to_change_the_running_installation(operation):
    report = uv_report()
    with pytest.raises(LifecycleError) as caught:
        check_not_self_mutation(operation, report, platform="win32", current_prefix=report.prefix)
    err = caught.value
    assert err.code is ExitCode.REFUSED
    assert err.manual_command == manual_command(operation, report)
    assert "fresh terminal" in (err.recovery or "")
    assert "Nothing was changed" in err.message
    assert err.details["reason"] == "windows_running_installation"


def test_windows_prefix_comparison_ignores_case_and_slashes():
    report = uv_report(prefix="C:/Tools/Velune-CLI")
    with pytest.raises(LifecycleError):
        check_not_self_mutation(
            "update", report, platform="win32", current_prefix="c:\\tools\\velune-cli"
        )


def test_windows_unresolvable_state_fails_closed(monkeypatch):
    from velune.lifecycle import safety

    def boom(_path):
        raise OSError("cannot resolve")

    monkeypatch.setattr(safety, "_norm", boom)
    with pytest.raises(LifecycleError) as caught:
        check_not_self_mutation("update", uv_report(), platform="win32", current_prefix="x")
    assert caught.value.code is ExitCode.REFUSED


def test_windows_a_clearly_different_environment_is_not_blocked_by_this_check():
    # The guard exists for the *running* installation only.
    check_not_self_mutation(
        "update",
        uv_report(prefix="C:/tools/velune-cli"),
        platform="win32",
        current_prefix="D:/elsewhere/venv",
    )


@pytest.mark.parametrize("platform", ["linux", "darwin", "freebsd14"])
def test_non_windows_has_no_self_mutation_check(platform):
    report = uv_report()
    check_not_self_mutation("update", report, platform=platform, current_prefix=report.prefix)


def test_guard_never_runs_anything(monkeypatch):
    import subprocess

    def forbidden(*a, **k):
        raise AssertionError("the guard must not start processes")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    report = uv_report()
    with pytest.raises(LifecycleError):
        check_not_self_mutation("update", report, platform="win32", current_prefix=report.prefix)
    check_not_self_mutation("update", report, platform="linux", current_prefix=report.prefix)


# ---- manual commands -------------------------------------------------------


def test_manual_commands_cover_the_spike_findings():
    plain = uv_report()
    assert manual_command("update", plain) == "uv tool upgrade velune-cli"
    assert manual_command("uninstall", plain) == "uv tool uninstall velune-cli"
    assert manual_command("repair", plain) == 'uv tool install --force "velune-cli==0.9.9"'

    pinned = uv_report(specifier="==0.9.9")  # `uv tool upgrade` says "Nothing to upgrade"
    assert manual_command("update", pinned) == "uv tool install --force velune-cli"

    extras = uv_report(specifier="==0.9.9", extras=("rag", "parsing"))  # reinstall drops extras
    assert manual_command("update", extras) == 'uv tool install --force "velune-cli[rag,parsing]"'
    assert (
        manual_command("repair", extras)
        == 'uv tool install --force "velune-cli[rag,parsing]==0.9.9"'
    )


def test_manual_command_never_embeds_unvalidated_text():
    nasty = uv_report(version="1.0; rm -rf /", extras=("rag", "x; echo hi", "$(whoami)"))
    command = manual_command("repair", nasty)
    assert ";" not in command and "$(" not in command and "rm" not in command
    assert command == 'uv tool install --force "velune-cli[rag]"'


def test_unknown_operation_is_rejected():
    with pytest.raises(ValueError):
        manual_command("purge", uv_report())
