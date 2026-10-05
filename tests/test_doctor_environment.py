"""`velune doctor` runs diagnostics, reports the environment, and only fails on core problems.

Regressions:
* bare `velune doctor` (the documented command) printed help instead of
  diagnosing anything — the checks were hidden behind `doctor check`;
* there was no environment summary (version, interpreter, OS/arch, install
  and data locations) — the first thing a support question needs;
* any failing optional integration (an unset/invalid provider key, Ollama
  not running) made the exit code non-zero, so "is my install OK?" could not
  be answered by scripts or CI.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from velune.cli.commands import doctor

runner = CliRunner()


def _fake(name: str, status: str):
    def _check() -> dict:
        return {"name": name, "status": status, "message": f"{name} is {status}"}

    _check.__name__ = "_check_" + name.lower().replace(" ", "_")
    return _check


def _core_ok():
    return [_fake(n, "ok") for n in sorted(doctor.CORE_CHECKS)]


@pytest.fixture
def use_checks(monkeypatch):
    def _set(checks):
        monkeypatch.setattr(doctor, "_all_checks", lambda: checks)

    return _set


def test_bare_doctor_runs_checks_not_help(use_checks):
    use_checks(_core_ok())
    result = runner.invoke(doctor.doctor_cmd, [])
    assert result.exit_code == 0, result.output
    assert "Environment" in result.output
    assert "Core installation is healthy" in result.output
    assert "Usage:" not in result.output


def test_help_still_shows_help():
    result = runner.invoke(doctor.doctor_cmd, ["--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_optional_failure_does_not_fail_the_install(use_checks):
    use_checks([*_core_ok(), _fake("Groq", "fail"), _fake("Ollama Connectivity", "warn")])
    result = runner.invoke(doctor.doctor_cmd, [])
    assert result.exit_code == 0, result.output
    assert "Core installation is healthy" in result.output
    assert "2 optional integrations need attention" in result.output


def test_core_failure_exits_non_zero(use_checks):
    checks = [c for c in _core_ok() if "Core Dependencies" not in c()["name"]]
    use_checks([*checks, _fake("Core Dependencies", "fail")])
    result = runner.invoke(doctor.doctor_cmd, [])
    assert result.exit_code == 1
    assert "Core installation has problems: Core Dependencies" in result.output


def test_raising_core_check_counts_as_core_failure(use_checks):
    def _check_core_dependencies():
        raise RuntimeError("boom")

    # A check that raises becomes an error row named after the function; the
    # verdict treats an errored *core* check as a core failure.
    _check_core_dependencies.__name__ = "_check_core_dependencies"
    results = doctor._run_checks([_check_core_dependencies])
    assert results[0]["status"] == "error"
    results[0]["name"] = "Core Dependencies"
    assert doctor._verdict(results)["core_healthy"] is False


def test_json_envelope_includes_environment(use_checks):
    use_checks([*_core_ok(), _fake("Groq", "fail")])
    result = runner.invoke(doctor.doctor_cmd, ["--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    env = payload["environment"]
    for key in (
        "velune_version",
        "python",
        "python_executable",
        "os",
        "architecture",
        "install_location",
        "velune_command",
        "config_dir",
        "data_dir",
    ):
        assert env[key], key
    assert payload["core_healthy"] is True
    assert payload["optional_attention"] == ["Groq"]
    assert len(payload["checks"]) == len(doctor.CORE_CHECKS) + 1


def test_check_subcommand_json_stays_a_plain_list(use_checks):
    # Backward compatibility for anything parsing `velune doctor check --json`.
    use_checks(_core_ok())
    result = runner.invoke(doctor.doctor_cmd, ["check", "--json"])
    assert result.exit_code == 0, result.output
    assert isinstance(json.loads(result.output), list)
