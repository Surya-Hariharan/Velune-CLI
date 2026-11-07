"""`velune update --check` (P2.2): version policy, orchestration, CLI, and read-only guarantees.

Tests that compare versions need a working ``packaging``. Where it is missing
(the damaged dev .venv) they skip rather than fake it; the
"packaging unavailable" behaviour itself is tested without it.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from velune.lifecycle import check as check_mod
from velune.lifecycle import versions
from velune.lifecycle.errors import ExitCode, LifecycleError
from velune.lifecycle.ownership import OwnershipKind, OwnershipReport

PY = (3, 12, 4)
REAL_CHECK = check_mod.run_update_check  # tests below replace the module attribute


def needs_packaging():
    pytest.importorskip("packaging.version")
    pytest.importorskip("packaging.specifiers")
    return versions.load_version_tools()


def rel(*items: str, **kw) -> dict:
    """Build a releases mapping: each version has one non-yanked file by default."""
    return {v: [{"yanked": False, "requires_python": ">=3.10"}] for v in items} | kw


def uv_report(**kw) -> OwnershipReport:
    base: dict = {
        "kind": OwnershipKind.UV_TOOL,
        "reasons": ("ok",),
        "prefix": "/t/velune-cli",
        "version": "0.9.9",
        "specifier": None,
        "extras": (),
    }
    return OwnershipReport(**(base | kw))


def run(report=None, releases=None, *, fetch=None, **kw):
    calls = {"fetch": 0}

    def default_fetch():
        calls["fetch"] += 1
        return releases if releases is not None else rel("0.9.9")

    def counting_fetch():
        calls["fetch"] += 1
        return fetch()

    result = REAL_CHECK(
        detect=lambda: report or uv_report(),
        fetch=counting_fetch if fetch else default_fetch,
        python=PY,
        platform=kw.pop("platform", "linux"),
        environ=kw.pop("environ", {}),
        **kw,
    )
    return result, calls


# ---- version policy ---------------------------------------------------------


def test_numeric_not_lexicographic_ordering():
    t = needs_packaging()
    assert (
        str(
            versions.select_latest(
                t, rel("0.9.9", "0.9.10", "0.10.0", "0.2.0"), python=PY, include_prereleases=False
            )
        )
        == "0.10.0"
    )


def test_compare_outcomes():
    t = needs_packaging()
    v = t.Version
    assert versions.compare(v("0.9.9"), v("0.9.10")) == "update"
    assert versions.compare(v("0.9.9"), v("0.9.9")) == "same"
    assert versions.compare(v("0.9.9"), v("0.9.8")) == "ahead"
    assert versions.compare(v("0.9.9+local"), v("0.9.9")) == "ahead"
    assert versions.compare(v("1.0.0rc1"), v("0.9.9")) == "ahead"
    assert versions.compare(v("1.0.0rc1"), v("1.0.0")) == "update"
    assert versions.compare(v("0.9.9.dev1"), v("0.9.9")) == "update"


def test_prereleases_are_excluded_unless_requested():
    t = needs_packaging()
    releases = rel("0.9.9", "1.0.0rc1", "1.0.0b2", "1.0.0.dev3")
    assert str(versions.select_latest(t, releases, python=PY, include_prereleases=False)) == "0.9.9"
    assert (
        str(versions.select_latest(t, releases, python=PY, include_prereleases=True)) == "1.0.0rc1"
    )


def test_yanked_and_empty_releases_are_never_targets():
    t = needs_packaging()
    releases = rel("0.9.9") | {
        "1.0.0": [{"yanked": True}, {"yanked": True}],
        "1.1.0": [],
        "1.2.0": "not a list",
        "1.3.0": [None, 5],
    }
    assert str(versions.select_latest(t, releases, python=PY, include_prereleases=False)) == "0.9.9"


def test_partly_yanked_release_is_still_usable():
    t = needs_packaging()
    releases = {"1.0.0": [{"yanked": True}, {"yanked": False}]}
    assert str(versions.select_latest(t, releases, python=PY, include_prereleases=False)) == "1.0.0"


def test_requires_python_filters_releases():
    t = needs_packaging()
    releases = {
        "1.0.0": [{"requires_python": ">=3.10"}],
        "2.0.0": [{"requires_python": ">=3.13"}],
        "3.0.0": [{"requires_python": "not valid"}],
        "4.0.0": [{"requires_python": 7}],
        "0.5.0": [{}],
    }
    assert str(versions.select_latest(t, releases, python=PY, include_prereleases=False)) == "1.0.0"
    assert (
        str(versions.select_latest(t, releases, python=(3, 13, 0), include_prereleases=False))
        == "2.0.0"
    )


def test_malformed_and_local_index_versions_are_ignored_not_guessed():
    t = needs_packaging()
    releases = rel("0.9.9", "banana", "1.x", "", "2.0.0+local")
    assert str(versions.select_latest(t, releases, python=PY, include_prereleases=False)) == "0.9.9"


def test_nothing_applicable_gives_none():
    t = needs_packaging()
    assert versions.select_latest(t, {}, python=PY, include_prereleases=False) is None
    assert versions.select_latest(t, rel("1.0.0rc1"), python=PY, include_prereleases=False) is None


@pytest.mark.parametrize("bad", ["", None, "banana", "1..2", "v-one"])
def test_installed_version_must_be_valid(bad):
    t = needs_packaging()
    with pytest.raises(LifecycleError) as caught:
        versions.parse_installed(t, bad)
    assert caught.value.code is ExitCode.ERROR


def test_printable_bounds_untrusted_text():
    assert versions.printable("1.0 \x1b[31m; rm -rf /" + "x" * 100).count(";") == 0
    assert len(versions.printable("a" * 500)) == 40


# ---- packaging unavailable (runs everywhere) --------------------------------


def _hide_packaging(monkeypatch):
    for name in ("packaging.version", "packaging.specifiers"):
        monkeypatch.setitem(sys.modules, name, None)  # import -> ImportError


def test_missing_packaging_is_reported_not_worked_around(monkeypatch):
    _hide_packaging(monkeypatch)
    fetched = []
    result = check_mod.run_update_check(
        detect=lambda: uv_report(), fetch=lambda: fetched.append(1) or {}, python=PY
    )
    assert result.exit_code is ExitCode.ERROR
    assert "'packaging' library" in result.message and "Nothing was changed" in result.message
    assert result.result["reason"] == "version_library_unavailable"
    assert fetched == []  # no network use when comparison is impossible


def test_missing_packaging_does_not_leak_import_error_text(monkeypatch):
    _hide_packaging(monkeypatch)
    blob = json.dumps(
        check_mod.run_update_check(detect=lambda: uv_report(), fetch=lambda: {}).to_dict()
    )
    assert "No module named" not in blob


# ---- ownership --------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        OwnershipKind.NOT_UV,
        OwnershipKind.EDITABLE,
        OwnershipKind.AMBIGUOUS,
        OwnershipKind.INDETERMINATE,
    ],
)
def test_anything_but_a_confirmed_uv_tool_is_refused_before_any_work(kind):
    fetched, loaded = [], []
    result = check_mod.run_update_check(
        detect=lambda: uv_report(kind=kind),
        load_tools=lambda: loaded.append(1),
        fetch=lambda: fetched.append(1) or {},
    )
    assert result.exit_code is ExitCode.REFUSED and result.exit_code == 3
    assert "Nothing was changed" in (result.recovery or "")
    assert result.installation["owned"] is False and result.installation["kind"] == kind.value
    assert fetched == [] and loaded == []


def test_detection_crash_is_an_error_not_a_traceback():
    def boom():
        raise RuntimeError("C:\\Users\\bob\\secret")

    result = check_mod.run_update_check(detect=boom, fetch=lambda: {})
    assert result.exit_code is ExitCode.ERROR
    assert "bob" not in json.dumps(result.to_dict())


# ---- command behaviour ------------------------------------------------------


def test_update_available_exit_10_with_next_step():
    needs_packaging()
    result, calls = run(releases=rel("0.9.9", "0.9.10"))
    assert result.exit_code is ExitCode.UPDATE_AVAILABLE and result.exit_code == 10
    assert result.ok and calls["fetch"] == 1
    assert "0.9.9 -> 0.9.10" in result.message and "pypi.org" in result.message
    assert result.manual_command == "uv tool upgrade velune-cli"
    assert result.result == {
        "installed_version": "0.9.9",
        "latest_version": "0.9.10",
        "update_available": True,
        "include_prereleases": False,
        "index": "pypi.org",
    }
    text = result.render_text()
    assert "Run: uv tool upgrade velune-cli" in text


def test_up_to_date_exit_0():
    needs_packaging()
    result, _ = run(releases=rel("0.9.8", "0.9.9"))
    assert result.exit_code is ExitCode.OK and result.manual_command is None
    assert "up to date" in result.message and result.result["update_available"] is False


def test_installed_newer_than_index():
    needs_packaging()
    result, _ = run(uv_report(version="1.0.0"), rel("0.9.9"))
    assert result.exit_code is ExitCode.OK
    assert "newer than the latest release" in result.message


def test_pre_flag_considers_prereleases_and_pins_exactly():
    needs_packaging()
    releases = rel("0.9.9", "1.0.0rc1")
    stable, _ = run(releases=releases)
    assert stable.exit_code is ExitCode.OK
    pre, _ = run(releases=releases, include_prereleases=True)
    assert pre.exit_code is ExitCode.UPDATE_AVAILABLE
    assert pre.manual_command == 'uv tool install --force "velune-cli==1.0.0rc1"'
    assert any("pre-release" in w for w in pre.warnings)
    assert pre.result["include_prereleases"] is True


def test_pinned_install_warns_and_uses_a_command_that_moves_the_pin():
    needs_packaging()
    result, _ = run(uv_report(specifier="==0.9.9", extras=("rag",)), rel("0.9.9", "0.9.10"))
    assert result.manual_command == 'uv tool install --force "velune-cli[rag]"'
    assert any("pinned" in w for w in result.warnings)


def test_local_file_install_warns_about_switching_source():
    needs_packaging()
    result, _ = run(uv_report(installed_from_local_file=True), rel("0.9.10"))
    assert any("local file" in w for w in result.warnings)
    assert result.manual_command.startswith("uv tool install --force")


def test_windows_guidance_is_manual_only():
    needs_packaging()
    win, _ = run(releases=rel("0.9.10"), platform="win32")
    assert "manual-only on Windows" in win.recovery and "fresh terminal" in win.recovery
    other, _ = run(releases=rel("0.9.10"), platform="linux")
    assert "Windows" not in other.recovery


def test_never_promises_that_velune_update_applies_updates():
    needs_packaging()
    text = run(releases=rel("0.9.10"))[0].render_text()
    assert "velune update" not in text


def test_index_override_variables_warn_without_values():
    needs_packaging()
    env = {"UV_INDEX_URL": "https://u:pw@corp/simple"}
    for releases in (rel("0.9.10"), rel("0.9.9")):
        result, _ = run(releases=releases, environ=env)
        assert any("UV_INDEX_URL" in w for w in result.warnings)
        assert "pw" not in json.dumps(result.to_dict())


def test_no_applicable_release_is_an_index_failure():
    needs_packaging()
    result, _ = run(releases=rel("1.0.0rc1"))
    assert (
        result.exit_code is ExitCode.NETWORK and result.result["reason"] == "no_applicable_release"
    )


def test_invalid_installed_version_stops_the_check():
    needs_packaging()
    result, calls = run(uv_report(version="not-a-version"), rel("0.9.10"))
    assert result.exit_code is ExitCode.ERROR and calls["fetch"] == 0
    assert result.result["reason"] == "installed_version_invalid"


def test_missing_installed_version_stops_the_check():
    needs_packaging()
    result, calls = run(uv_report(version=None), rel("0.9.10"))
    assert result.exit_code is ExitCode.ERROR and calls["fetch"] == 0


def test_network_failure_is_exit_4_and_says_nothing_changed():
    needs_packaging()

    def down():
        raise LifecycleError(
            ExitCode.NETWORK, "Could not connect to pypi.org. Nothing was changed."
        )

    result, _ = run(fetch=down)
    assert result.exit_code is ExitCode.NETWORK and result.exit_code == 4
    assert not result.ok


def test_unexpected_failure_in_fetch_is_contained():
    needs_packaging()

    def boom():
        raise ValueError("https://user:pw@host/")

    result, _ = run(fetch=boom)
    assert result.exit_code is ExitCode.ERROR and "pw@host" not in json.dumps(result.to_dict())


def test_ctrl_c_is_cancelled():
    needs_packaging()

    def interrupt():
        raise KeyboardInterrupt

    result, _ = run(fetch=interrupt)
    assert result.exit_code is ExitCode.CANCELLED


def test_json_result_has_only_the_designed_top_level_fields():
    needs_packaging()
    data = run(releases=rel("0.9.10"))[0].to_dict()
    assert set(data) == {
        "command",
        "status",
        "exit_code",
        "message",
        "installation",
        "plan",
        "result",
        "warnings",
        "recovery",
    }
    assert data["command"] == "update" and data["status"] == "update_available"
    assert data["installation"] == {"kind": "uv_tool", "owned": True, "path": "/t/velune-cli"}
    json.dumps(data)


# ---- read-only guarantees ---------------------------------------------------


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_check_changes_nothing_on_disk_and_starts_no_process(tmp_path, monkeypatch):
    needs_packaging()
    env_dir = tmp_path / "tools" / "velune-cli"
    env_dir.mkdir(parents=True)
    (env_dir / "uv-receipt.toml").write_text('[tool]\nrequirements = [{ name = "velune-cli" }]\n')
    (env_dir / "pyvenv.cfg").write_text("home = x\n")
    data = tmp_path / "data"
    (data / "workspaces").mkdir(parents=True)
    (data / "workspaces" / "keep.db").write_bytes(b"memory")
    (tmp_path / "velune.toml").write_text('default_provider = "x"\n')
    (tmp_path / "credentials.json").write_bytes(b"encrypted-blob")
    for var in (
        "HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
    ):
        monkeypatch.setenv(var, str(tmp_path))
    monkeypatch.setenv("VELUNE_DATA_HOME", str(data))
    monkeypatch.chdir(tmp_path)

    def forbidden(*a, **k):
        raise AssertionError("update --check must not start a process")

    for target in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, target, forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    from velune.lifecycle import journal

    monkeypatch.setattr(journal, "write_journal", forbidden)

    before = _tree_digest(tmp_path)
    for releases in (rel("0.9.10"), rel("0.9.9"), {}):
        run(releases=releases)
    after = _tree_digest(tmp_path)

    assert after == before
    assert not journal.default_journal_path().exists()
    assert not (data / "lifecycle").exists()


def test_the_check_module_has_no_mutating_uv_calls():
    """Static guard: the modules the check runs must not even mention mutating uv verbs as argv."""
    root = Path(__file__).resolve().parent.parent / "velune" / "lifecycle"
    for name in ("check.py", "index.py", "versions.py"):
        source = (root / name).read_text(encoding="utf-8")
        for verb in ('"install"', '"upgrade"', '"uninstall"', "import subprocess"):
            assert verb not in source, f"{name} contains {verb}"


# ---- CLI --------------------------------------------------------------------


def _cli() -> typer.Typer:
    from velune.cli.commands.update import update_cmd

    app = typer.Typer()
    app.command()(update_cmd)
    return app


def test_without_check_nothing_runs_and_exit_is_usage(monkeypatch):
    def forbidden(**k):
        raise AssertionError("must not run the check")

    monkeypatch.setattr(check_mod, "run_update_check", forbidden)
    res = CliRunner().invoke(_cli(), [])
    assert res.exit_code == 2
    assert "not available yet" in res.output and "--check" in res.output


def test_cli_maps_results_to_exit_codes_and_streams(monkeypatch):
    needs_packaging()
    cases = {
        ("0.9.10", 10): "Update available",
        ("0.9.9", 0): "up to date",
    }
    for (latest, code), phrase in cases.items():
        monkeypatch.setattr(
            check_mod,
            "run_update_check",
            lambda include_prereleases=False, latest=latest: run(releases=rel(latest))[0],
        )
        res = CliRunner().invoke(_cli(), ["--check"])
        assert res.exit_code == code and phrase in res.output


def test_failures_go_to_stderr_and_successes_to_stdout(capsys):
    from velune.cli.commands.update import _emit
    from velune.lifecycle.errors import LifecycleResult

    with pytest.raises(typer.Exit) as failed:
        _emit(LifecycleResult("update", exit_code=ExitCode.NETWORK, message="offline"), False)
    out = capsys.readouterr()
    assert failed.value.exit_code == 4 and "offline" in out.err and "offline" not in out.out

    with pytest.raises(typer.Exit) as fine:
        _emit(LifecycleResult("update", message="all good"), False)
    out = capsys.readouterr()
    assert fine.value.exit_code == 0 and "all good" in out.out and not out.err


def test_cli_json_output_is_valid_and_matches(monkeypatch):
    needs_packaging()
    monkeypatch.setattr(
        check_mod,
        "run_update_check",
        lambda include_prereleases=False: run(releases=rel("0.9.10"))[0],
    )
    res = CliRunner().invoke(_cli(), ["--check", "--json"])
    assert res.exit_code == 10
    payload = json.loads(res.output)
    assert payload["status"] == "update_available" and payload["exit_code"] == 10


def test_cli_passes_pre_through(monkeypatch):
    seen = {}

    def fake(include_prereleases=False):
        seen["pre"] = include_prereleases
        return check_mod.LifecycleResult("update", message="ok")

    monkeypatch.setattr(check_mod, "run_update_check", fake)
    CliRunner().invoke(_cli(), ["--check", "--pre"])
    assert seen == {"pre": True}


def test_registered_in_the_command_table():
    from velune.cli.registry import COMMAND_SPECS

    (spec,) = [s for s in COMMAND_SPECS if s.name == "update"]
    assert spec.kind == "command" and spec.bootstrap == "light"
    assert callable(getattr(importlib.import_module(spec.module), spec.attr))


def test_real_cli_wiring_help_and_usage_error():
    """End to end through the lazy registry. Neither call can reach the network or uv."""
    base = [sys.executable, "-m", "velune"]
    env = {**os.environ, "NO_COLOR": "1", "COLUMNS": "200", "PYTHONIOENCODING": "utf-8"}
    kwargs = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": env,
        "timeout": 120,
    }
    helped = subprocess.run(base + ["update", "--help"], **kwargs)
    assert helped.returncode == 0 and "--check" in helped.stdout
    bare = subprocess.run(base + ["update"], **kwargs)
    assert bare.returncode == 2 and "not available yet" in (bare.stdout + bare.stderr)
