"""F1: uv is never resolved from the current directory or a relative PATH entry.

``shutil.which`` on Windows with Python 3.10/3.11 returns ``.\\uv.bat`` from the cwd, and
a ``.bat`` runs through ``cmd.exe``. Lifecycle code therefore resolves uv with
:mod:`velune.lifecycle.uvpath`. The tests plant launchers that write a marker file; the
marker must never appear. Everything happens in temp directories; no real uv runs.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from velune.lifecycle import apply as apply_mod
from velune.lifecycle import check as check_mod
from velune.lifecycle import ownership as own
from velune.lifecycle import repair as repair_mod
from velune.lifecycle import uninstall as uninstall_mod
from velune.lifecycle import uvpath
from velune.lifecycle.errors import ExitCode
from velune.lifecycle.ownership import OwnershipKind, OwnershipReport

ON_WINDOWS = sys.platform.startswith("win")
REAL_CHECK = check_mod.run_update_check


def plant(directory: Path, name: str) -> Path:
    """A launcher that would leave ``marker.txt`` next to it if it ever ran."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    marker = directory / "marker.txt"
    if name.endswith((".bat", ".cmd")):
        path.write_text(f'@echo off\r\necho ran> "{marker}"\r\n', encoding="utf-8")
    else:
        path.write_text(f"#!/bin/sh\necho ran > '{marker}'\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def fake_uv(directory: Path, windows: bool) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("uv.exe" if windows else "uv")
    path.write_bytes(b"not a real binary")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def joined(*parts, windows: bool = False) -> str:
    """PATH text for the *host* (tests emulate the other platform's rules, not its separator)."""
    return os.pathsep.join(str(p) for p in parts)


@pytest.fixture
def cwd(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.fixture
def safe(tmp_path):
    d = tmp_path / "safe-bin"
    d.mkdir()
    return d


# ---- the resolver itself --------------------------------------------------------------


@pytest.mark.parametrize("name", ["uv.bat", "uv.cmd", "uv.exe", "uv", "uv.com"])
def test_a_launcher_in_the_cwd_is_never_selected(cwd, name):
    plant(cwd, name)
    for plat in ("win32", "linux"):
        for path_env in ("", ".", joined(cwd, windows=plat == "win32")):
            found = uvpath.find_uv(
                path_env=path_env, cwd=str(cwd), platform=plat, pathsep=os.pathsep
            )
            assert found.path is None
    assert not (cwd / "marker.txt").exists()


def test_cwd_launcher_is_reported_as_untrusted_not_missing(cwd):
    plant(cwd, "uv.bat")
    found = uvpath.find_uv(path_env="", cwd=str(cwd), platform="win32", pathsep=os.pathsep)
    assert found.path is None and found.reason == uvpath.UNTRUSTED


def test_nothing_anywhere_is_reported_as_not_found(cwd, safe):
    found = uvpath.find_uv(path_env=str(safe), cwd=str(cwd), platform="linux", pathsep=os.pathsep)
    assert found.path is None and found.reason == uvpath.NOT_FOUND


@pytest.mark.parametrize("name", ["uv.bat", "uv.cmd", "uv.com", "uv"])
def test_windows_accepts_only_uv_exe_even_outside_the_cwd(cwd, safe, name):
    plant(safe, name)
    found = uvpath.find_uv(path_env=str(safe), cwd=str(cwd), platform="win32", pathsep=os.pathsep)
    assert found.path is None
    assert not (safe / "marker.txt").exists()


def test_windows_accepts_uv_exe_outside_the_cwd(cwd, safe):
    exe = fake_uv(safe, windows=True)
    found = uvpath.find_uv(path_env=str(safe), cwd=str(cwd), platform="win32", pathsep=os.pathsep)
    assert found.path == os.path.abspath(exe) and os.path.isabs(found.path)


@pytest.mark.skipif(ON_WINDOWS, reason="POSIX execute bits")
def test_posix_accepts_an_executable_uv_outside_the_cwd(cwd, safe):
    exe = fake_uv(safe, windows=False)
    found = uvpath.find_uv(path_env=str(safe), cwd=str(cwd), platform="linux", pathsep=os.pathsep)
    assert found.path == os.path.abspath(exe)


@pytest.mark.skipif(ON_WINDOWS, reason="POSIX execute bits")
def test_posix_ignores_a_non_executable_uv(cwd, safe):
    path = fake_uv(safe, windows=False)
    path.chmod(0o644)
    assert (
        uvpath.find_uv(path_env=str(safe), cwd=str(cwd), platform="linux", pathsep=os.pathsep).path
        is None
    )


@pytest.mark.parametrize("plat", ["win32", "linux"])
def test_relative_empty_and_dotdot_path_entries_are_skipped(cwd, safe, plat):
    windows = plat == "win32"
    fake_uv(cwd / "rel", windows)  # reachable only through a relative entry
    fake_uv(safe, windows)
    entries = ["", ".", "rel", "./rel", "..", str(safe / ".." / "safe-bin")]
    found = uvpath.find_uv(
        path_env=joined(*entries, windows=windows), cwd=str(cwd), platform=plat, pathsep=os.pathsep
    )
    assert found.path is None  # none of those entries is trustworthy


@pytest.mark.parametrize("plat", ["win32", "linux"])
def test_an_absolute_entry_inside_the_cwd_is_skipped_but_a_later_safe_one_wins(cwd, safe, plat):
    windows = plat == "win32"
    inside = cwd / "tools"
    fake_uv(inside, windows)
    good = fake_uv(safe, windows)
    path_env = joined(inside, cwd, safe, windows=windows)
    found = uvpath.find_uv(path_env=path_env, cwd=str(cwd), platform=plat, pathsep=os.pathsep)
    assert found.path == os.path.abspath(good)


def test_a_safe_directory_with_a_link_into_the_cwd_is_refused(cwd, safe):
    target = fake_uv(cwd, windows=ON_WINDOWS)
    link = safe / target.name
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need a privilege this environment lacks")
    found = uvpath.find_uv(
        path_env=str(safe),
        cwd=str(cwd),
        platform="win32" if ON_WINDOWS else "linux",
        pathsep=os.pathsep,
    )
    assert found.path is None


def test_trusted_which_only_resolves_uv(monkeypatch):
    assert uvpath.trusted_which("python") is None


def test_messages_carry_no_paths():
    for reason in (uvpath.UNTRUSTED, uvpath.NOT_FOUND, uvpath.CWD_UNAVAILABLE, None):
        text = uvpath.uv_problem_message(reason)
        assert "\\" not in text and "/" not in text.replace("PATH", "")


# ---- every lifecycle entry point uses it -------------------------------------------------


def uv_tool_report() -> OwnershipReport:
    return OwnershipReport(
        kind=OwnershipKind.UV_TOOL,
        reasons=("ok",),
        prefix="/tools/velune-cli",
        version="0.9.9",
        specifier=None,
        extras=(),
    )


@pytest.fixture
def hostile(cwd, monkeypatch):
    """cwd holds every planted launcher and PATH points only at the cwd and relative bits."""
    for name in ("uv.bat", "uv.cmd", "uv.exe", "uv"):
        plant(cwd, name)
    monkeypatch.setenv("PATH", joined("", ".", cwd, windows=ON_WINDOWS))

    def no_process(*a, **k):
        raise AssertionError("a process was started")

    monkeypatch.setattr(own.subprocess, "run", no_process)
    monkeypatch.setattr(apply_mod.subprocess, "Popen", no_process)
    return cwd


def test_resolve_uv_tool_dir_does_not_run_a_planted_uv(hostile):
    path, problem = own.resolve_uv_tool_dir()
    assert path is None and "trusted" in (problem or "") or "untrusted" in (problem or "")
    assert not (hostile / "marker.txt").exists()


def test_ownership_detection_degrades_to_indeterminate_and_runs_nothing(hostile):
    report = own.detect_ownership()
    assert report.kind is not OwnershipKind.UV_TOOL
    assert not (hostile / "marker.txt").exists()


def test_update_check_cli_never_runs_a_planted_uv(hostile, monkeypatch):
    import typer

    from velune.cli.commands.update import update_cmd

    monkeypatch.setattr(
        check_mod,
        "run_update_check",
        lambda include_prereleases=False: REAL_CHECK(
            include_prereleases=include_prereleases,
            fetch=lambda: {"99.0.0": [{"yanked": False, "requires_python": ">=3.10"}]},
        ),
    )
    app = typer.Typer()
    app.command()(update_cmd)
    result = CliRunner().invoke(app, ["--check"])
    assert result.exit_code in {0, 1, 10}  # the check itself still works, read-only
    assert not (hostile / "marker.txt").exists()
    assert "marker" not in result.output


def _common(tmp_path):
    return {
        "detect": lambda: uv_tool_report(),
        "journal_path": tmp_path / "lifecycle" / "journal.json",
        "python": (3, 12, 4),
        "platform": "linux",
        "current_prefix": "/somewhere/else",
    }


def test_update_refuses_when_only_a_planted_uv_exists(hostile, tmp_path):
    kw = _common(tmp_path) | {
        "fetch": lambda: {"0.9.9": [{"yanked": False}], "0.9.10": [{"yanked": False}]},
        "environ": {},
    }
    result = apply_mod.run_update(**kw)
    assert result.exit_code is ExitCode.REFUSED and result.result["reason"] == "uv_not_found"
    assert not (hostile / "marker.txt").exists()
    assert not (tmp_path / "lifecycle").exists()  # nothing journaled, nothing changed


def test_repair_refuses_when_only_a_planted_uv_exists(hostile, tmp_path):
    kw = _common(tmp_path) | {"environ": {}}
    result = repair_mod.run_repair(**kw)
    assert result.exit_code is ExitCode.REFUSED and result.result["reason"] == "uv_not_found"
    assert not (hostile / "marker.txt").exists()
    assert not (tmp_path / "lifecycle").exists()


def test_uninstall_refuses_when_only_a_planted_uv_exists(hostile, tmp_path):
    kw = _common(tmp_path) | {"assume_yes": True}
    result = uninstall_mod.run_uninstall(**kw)
    assert result.exit_code is ExitCode.REFUSED and result.result["reason"] == "uv_not_found"
    assert not (hostile / "marker.txt").exists()
    assert not (tmp_path / "lifecycle").exists()


def test_legitimate_uv_outside_the_cwd_is_still_used(cwd, safe, monkeypatch):
    exe = fake_uv(safe, windows=ON_WINDOWS)
    monkeypatch.setenv("PATH", str(safe))
    seen = []

    class Done:
        returncode = 0
        stdout = str(safe / "tools") + "\n"

    def run(argv, **kw):
        seen.append(list(argv))
        return Done()

    path, problem = own.resolve_uv_tool_dir(run=run)
    assert problem is None and path == safe / "tools"
    assert seen == [[os.path.abspath(exe), "tool", "dir"]]


def test_defaults_are_the_safe_resolver():
    import inspect

    for fn in (apply_mod.run_update, repair_mod.run_repair, uninstall_mod.run_uninstall):
        assert inspect.signature(fn).parameters["which"].default is uvpath.trusted_which


# ---- R1: alias spellings of the cwd are refused by filesystem identity -----------------------

BS = "\\"


def real_uv_copy(directory: Path) -> Path:
    """A harmless stand-in that only has to *exist* as a regular file named uv.exe."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "uv.exe"
    path.write_bytes(b"MZ not really")
    return path


def verbatim(p: Path) -> str:
    return BS * 2 + "?" + BS + str(p)


def admin_share(p: Path) -> str:
    drive = str(p)[0]
    return BS * 2 + "localhost" + BS + drive + "$" + str(p)[2:]


def verbatim_unc(p: Path) -> str:
    drive = str(p)[0]
    return BS * 2 + "?" + BS + "UNC" + BS + "localhost" + BS + drive + "$" + str(p)[2:]


def make_junction(link: Path, target: Path) -> None:
    import subprocess

    done = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
    )
    if done.returncode != 0:
        pytest.skip("could not create a junction here")


needs_windows = pytest.mark.skipif(not ON_WINDOWS, reason="Windows path aliases")


@needs_windows
@pytest.mark.parametrize("alias", [verbatim, admin_share, verbatim_unc], ids=lambda f: f.__name__)
def test_alias_spellings_of_the_cwd_are_refused(cwd, alias):
    planted = real_uv_copy(cwd)
    entry = alias(cwd)
    if not os.path.exists(os.path.join(entry, "uv.exe")):
        pytest.skip("this alias is not reachable on this machine")
    assert os.path.samestat(os.stat(entry), os.stat(cwd))  # really the same directory
    found = uvpath.find_uv(path_env=entry, cwd=str(cwd), platform="win32", pathsep=os.pathsep)
    assert found.path is None and found.reason == uvpath.UNTRUSTED
    assert planted.exists()


@needs_windows
def test_alias_of_a_subdirectory_of_the_cwd_is_refused_too(cwd):
    real_uv_copy(cwd / "tools")
    found = uvpath.find_uv(
        path_env=verbatim(cwd / "tools"), cwd=str(cwd), platform="win32", pathsep=os.pathsep
    )
    assert found.path is None


@needs_windows
def test_an_unsafe_alias_followed_by_a_safe_entry_resolves_to_the_safe_one(cwd, safe):
    real_uv_copy(cwd)
    good = real_uv_copy(safe)
    path_env = os.pathsep.join([verbatim(cwd), safe.as_posix().replace("/", BS)])
    found = uvpath.find_uv(path_env=path_env, cwd=str(cwd), platform="win32", pathsep=os.pathsep)
    assert found.path == os.path.abspath(good)


@needs_windows
@pytest.mark.parametrize("alias", [verbatim, admin_share], ids=lambda f: f.__name__)
def test_an_alias_of_a_safe_directory_still_resolves(cwd, safe, alias):
    real_uv_copy(safe)
    entry = alias(safe)
    if not os.path.exists(os.path.join(entry, "uv.exe")):
        pytest.skip("this alias is not reachable on this machine")
    found = uvpath.find_uv(path_env=entry, cwd=str(cwd), platform="win32", pathsep=os.pathsep)
    assert found.path is not None and os.path.samefile(found.path, safe / "uv.exe")


@needs_windows
def test_a_junction_to_a_safe_directory_still_resolves(cwd, safe, tmp_path):
    real_uv_copy(safe)
    link = tmp_path / "linked-bin"
    make_junction(link, safe)
    found = uvpath.find_uv(path_env=str(link), cwd=str(cwd), platform="win32", pathsep=os.pathsep)
    assert found.path is not None and os.path.samefile(found.path, safe / "uv.exe")


@needs_windows
def test_a_junction_pointing_into_the_cwd_is_refused(cwd, tmp_path):
    real_uv_copy(cwd)
    link = tmp_path / "sneaky-bin"
    make_junction(link, cwd)
    found = uvpath.find_uv(path_env=str(link), cwd=str(cwd), platform="win32", pathsep=os.pathsep)
    assert found.path is None


def test_missing_file_and_unusable_entries_are_a_clean_refusal(cwd, safe, tmp_path):
    a_file = tmp_path / "not-a-dir"
    a_file.write_text("x")
    entries = [tmp_path / "does-not-exist", a_file, a_file / "sub"]
    plat = "win32" if ON_WINDOWS else "linux"
    found = uvpath.find_uv(
        path_env=os.pathsep.join(map(str, entries)),
        cwd=str(cwd),
        platform=plat,
        pathsep=os.pathsep,
    )
    assert found.path is None and found.reason == uvpath.NOT_FOUND


# Portable: only the identity check can refuse these (the string checks are disabled).


@pytest.fixture
def string_checks_off(monkeypatch):
    monkeypatch.setattr(uvpath, "_at_or_below", lambda candidate, base: False)
    monkeypatch.setattr(uvpath, "_entry_is_safe", lambda entry, cwd: True)
    monkeypatch.setattr(uvpath.os.path, "realpath", lambda p: p)


def _portable(path_env: str, cwd: Path):
    plat = "win32" if ON_WINDOWS else "linux"
    return uvpath.find_uv(path_env=path_env, cwd=str(cwd), platform=plat, pathsep=os.pathsep)


def test_identity_alone_refuses_the_cwd_itself(cwd, string_checks_off):
    fake_uv(cwd, windows=ON_WINDOWS)
    found = _portable(str(cwd), cwd)
    assert found.path is None and found.reason == uvpath.UNTRUSTED


def test_identity_alone_refuses_a_subdirectory_of_the_cwd(cwd, string_checks_off):
    fake_uv(cwd / "deep" / "er", windows=ON_WINDOWS)
    assert _portable(str(cwd / "deep" / "er"), cwd).path is None


def test_identity_alone_still_accepts_a_directory_outside_the_cwd(cwd, safe, string_checks_off):
    good = fake_uv(safe, windows=ON_WINDOWS)
    assert _portable(str(safe), cwd).path == os.path.abspath(good)


def test_identity_check_fails_closed_when_an_ancestor_cannot_be_inspected(
    cwd, safe, string_checks_off, monkeypatch
):
    fake_uv(safe, windows=ON_WINDOWS)
    real_stat = os.stat
    parent = str(safe.parent)

    def flaky(path, *a, **k):
        if os.path.normcase(str(path)) == os.path.normcase(parent):
            raise PermissionError("denied")
        return real_stat(path, *a, **k)

    monkeypatch.setattr(uvpath.os, "stat", flaky)
    found = _portable(str(safe), cwd)
    assert found.path is None and found.reason == uvpath.UNTRUSTED


def test_identity_check_fails_closed_when_the_cwd_cannot_be_inspected(safe, tmp_path):
    fake_uv(safe, windows=ON_WINDOWS)
    gone = tmp_path / "deleted-cwd"  # never created
    plat = "win32" if ON_WINDOWS else "linux"
    found = uvpath.find_uv(path_env=str(safe), cwd=str(gone), platform=plat, pathsep=os.pathsep)
    assert found.path is None


def test_a_symlink_to_the_cwd_is_refused_but_one_to_a_safe_place_is_not(cwd, safe, tmp_path):
    fake_uv(cwd, windows=ON_WINDOWS)
    good = fake_uv(safe, windows=ON_WINDOWS)
    bad_link, good_link = tmp_path / "to-cwd", tmp_path / "to-safe"
    try:
        os.symlink(cwd, bad_link, target_is_directory=True)
        os.symlink(safe, good_link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need a privilege this environment lacks")
    assert _portable(str(bad_link), cwd).path is None
    found = _portable(str(good_link), cwd)
    assert found.path is not None and os.path.samefile(found.path, good)


# ---- R1.1: filesystem errors never escape; inode 0 is "unknown"; cwd above uv is refused ----

PLAT = "win32" if ON_WINDOWS else "linux"


def _find(path_env, cwd=None):
    return uvpath.find_uv(path_env=path_env, cwd=cwd, platform=PLAT, pathsep=os.pathsep)


@pytest.mark.parametrize(
    "error", [FileNotFoundError("cwd deleted"), OSError("cwd unreadable"), ValueError("bad cwd")]
)
def test_an_unavailable_cwd_is_a_controlled_refusal(safe, monkeypatch, error):
    fake_uv(safe, windows=ON_WINDOWS)

    def broken():
        raise error

    monkeypatch.setattr(uvpath.os, "getcwd", broken)
    found = _find(str(safe))  # cwd=None -> asks the process
    assert found.path is None and found.reason == uvpath.CWD_UNAVAILABLE
    assert uvpath.trusted_which("uv") is None


@pytest.mark.skipif(ON_WINDOWS, reason="Windows will not delete a process's cwd")
def test_a_really_deleted_cwd_is_a_controlled_refusal(tmp_path, safe, monkeypatch):
    fake_uv(safe, windows=False)
    doomed = tmp_path / "doomed"
    doomed.mkdir()
    monkeypatch.chdir(doomed)
    doomed.rmdir()
    found = _find(str(safe))
    assert found.path is None and found.reason == uvpath.CWD_UNAVAILABLE


def test_ownership_check_survives_an_unavailable_cwd(safe, monkeypatch):
    fake_uv(safe, windows=ON_WINDOWS)
    monkeypatch.setenv("PATH", str(safe))

    def broken():
        raise FileNotFoundError("cwd deleted")

    def must_not_run(*a, **k):  # pragma: no cover - failing here means uv was started
        raise AssertionError("uv must not run when the cwd cannot be inspected")

    monkeypatch.setattr(uvpath.os, "getcwd", broken)
    path, problem = own.resolve_uv_tool_dir(run=must_not_run)
    assert path is None and "current directory" in problem


def test_unavailable_cwd_message_is_actionable():
    text = uvpath.uv_problem_message(uvpath.CWD_UNAVAILABLE)
    assert "current directory" in text and "existing directory" in text


def test_a_nul_byte_in_a_path_entry_is_skipped_not_raised(cwd, safe):
    good = fake_uv(safe, windows=ON_WINDOWS)
    poisoned = str(safe) + "\0" + "x"
    assert _find(poisoned, str(cwd)).path is None
    assert _find(poisoned, str(cwd)).reason == uvpath.NOT_FOUND
    found = _find(os.pathsep.join([poisoned, str(safe)]), str(cwd))
    assert found.path == os.path.abspath(good)


def test_a_stat_error_on_the_candidate_is_a_clean_not_found(cwd, safe, monkeypatch):
    fake_uv(safe, windows=ON_WINDOWS)
    real_stat = os.stat

    def deny_candidate(path, *a, **k):
        if os.path.basename(str(path)).lower().startswith("uv"):
            raise PermissionError("denied")
        return real_stat(path, *a, **k)

    monkeypatch.setattr(uvpath.os, "stat", deny_candidate)
    found = _find(str(safe), str(cwd))
    assert found.path is None and found.reason == uvpath.NOT_FOUND


@pytest.mark.parametrize("error", [OSError("boom"), ValueError("embedded null byte")])
def test_a_realpath_error_refuses_instead_of_raising(cwd, safe, monkeypatch, error):
    fake_uv(safe, windows=ON_WINDOWS)

    def broken(path):
        raise error

    monkeypatch.setattr(uvpath.os.path, "realpath", broken)
    found = _find(str(safe), str(cwd))
    assert found.path is None and found.reason == uvpath.UNTRUSTED


def test_a_value_error_while_comparing_identities_refuses(cwd, safe, monkeypatch):
    fake_uv(safe, windows=ON_WINDOWS)
    real_stat = os.stat
    parent = os.path.normcase(str(safe.parent))

    def odd(path, *a, **k):
        if os.path.normcase(str(path)) == parent:
            raise ValueError("embedded null byte")
        return real_stat(path, *a, **k)

    monkeypatch.setattr(uvpath.os, "stat", odd)
    found = _find(str(safe), str(cwd))
    assert found.path is None and found.reason == uvpath.UNTRUSTED


# -- inode 0 is never a reliable match ---------------------------------------------------


class FakeStat:
    def __init__(self, st, *, ino=None, dev=None):
        self.st_mode, self.st_size, self.st_mtime_ns = st.st_mode, st.st_size, st.st_mtime_ns
        self.st_ino = st.st_ino if ino is None else ino
        self.st_dev = st.st_dev if dev is None else dev


def test_same_directory_rules():
    base = os.stat(os.getcwd())
    other_dev = FakeStat(base, dev=base.st_dev + 1)
    zero_a, zero_b = FakeStat(base, ino=0), FakeStat(base, ino=0)
    assert uvpath._same_directory(base, base) is True
    assert uvpath._same_directory(base, other_dev) is False  # different device: definite no
    assert uvpath._same_directory(zero_a, zero_b) is None  # same device, no usable inode
    assert uvpath._same_directory(base, zero_b) is None
    assert uvpath._same_directory(zero_a, base) is None


def _directories_report_inode_zero(monkeypatch, dev_for=None):
    real_stat = os.stat

    def patched(path, *a, **k):
        st = real_stat(path, *a, **k)
        if stat.S_ISDIR(st.st_mode):
            return FakeStat(st, ino=0, dev=None if dev_for is None else dev_for(str(path), st))
        return st

    monkeypatch.setattr(uvpath.os, "stat", patched)


def test_inode_zero_never_makes_unrelated_directories_match(cwd, safe, monkeypatch):
    fake_uv(safe, windows=ON_WINDOWS)
    _directories_report_inode_zero(monkeypatch)
    found = _find(str(safe), str(cwd))
    # Identity is unknown, not "same" and not "different": fail closed, and say why it is safe.
    assert found.path is None and found.reason == uvpath.UNTRUSTED


def test_inode_zero_on_another_device_is_still_definitely_different(cwd, safe, monkeypatch):
    good = fake_uv(safe, windows=ON_WINDOWS)
    cwd_key = os.path.normcase(str(cwd))

    def dev_for(path, st):
        return st.st_dev + 1 if os.path.normcase(path) == cwd_key else st.st_dev

    _directories_report_inode_zero(monkeypatch, dev_for)
    found = _find(str(safe), str(cwd))
    assert found.path == os.path.abspath(good)


def test_inode_zero_does_not_weaken_refusing_the_cwd_itself(cwd, monkeypatch):
    fake_uv(cwd, windows=ON_WINDOWS)
    _directories_report_inode_zero(monkeypatch)
    assert _find(str(cwd), str(cwd)).path is None


# -- a cwd that contains uv is refused, as documented -------------------------------------


def test_cwd_above_a_legitimate_uv_is_refused_with_actionable_message(tmp_path):
    home = tmp_path / "home"
    uv_dir = home / ".local" / "bin"
    good = fake_uv(uv_dir, windows=ON_WINDOWS)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    refused = _find(str(uv_dir), str(home))
    assert refused.path is None and refused.reason == uvpath.UNTRUSTED
    text = uvpath.uv_problem_message(refused.reason)
    assert "security restriction" in text and "run the command again" in text
    # The same uv is fine from a directory that does not contain it.
    assert _find(str(uv_dir), str(elsewhere)).path == os.path.abspath(good)


def test_cwd_above_uv_is_refused_by_identity_alone_too(tmp_path, string_checks_off):
    home = tmp_path / "home"
    fake_uv(home / ".local" / "bin", windows=ON_WINDOWS)
    assert _find(str(home / ".local" / "bin"), str(home)).path is None


# ---- R1.1 follow-up: the resolver's reason is what the user reads, not "reinstall" -----------


def _uv_tool_evidence(tool_dir_problem: str) -> own.OwnershipEvidence:
    """A genuine-looking uv tool whose uv simply could not be used (so tool_dir is unknown)."""
    return own.OwnershipEvidence(
        prefix=Path("/tools/velune-cli"),
        tool_dir=None,
        tool_dir_problem=tool_dir_problem,
        dist_found=True,
        version="0.9.9",
        dist_in_prefix=True,
        installer="uv",
        direct_url="none",
        receipt_state="ok",
        receipt=own.ReceiptInfo(specifier=None, extras=(), path=None, python=None),
        receipt_problem=None,
    )


@pytest.fixture
def refused_report(hostile):
    """The report produced when the only uv is under the cwd: real resolver, real evaluation."""
    _, problem = own.resolve_uv_tool_dir()
    assert problem and "security restriction" in problem
    return own.evaluate_ownership(_uv_tool_evidence(problem))


def test_a_resolver_problem_is_carried_on_the_ownership_report(refused_report):
    assert refused_report.kind is OwnershipKind.INDETERMINATE
    assert (
        refused_report.uv_problem
        and "directory that does not contain uv" in refused_report.uv_problem
    )
    assert refused_report.mutation_allowed is False


def test_other_indeterminate_reports_carry_no_uv_problem():
    report = own.evaluate_ownership(
        own.OwnershipEvidence(
            prefix=Path("/x"),
            tool_dir=None,
            tool_dir_problem="irrelevant",
            dist_found=False,
            version=None,
            dist_in_prefix=None,
            installer=None,
            direct_url="none",
            receipt_state="missing",
            receipt=None,
            receipt_problem=None,
        )
    )
    assert report.kind is OwnershipKind.INDETERMINATE and report.uv_problem is None


def _assert_actionable(text: str):
    assert "security restriction" in text
    assert "run the command again from a directory that does not contain uv" in text
    assert "reinstall" not in text.lower()


@pytest.mark.parametrize("runner", ["update", "repair", "uninstall"])
def test_text_mode_refusal_shows_the_resolver_reason(runner, refused_report, tmp_path):
    base = _common(tmp_path) | {"detect": lambda: refused_report}
    if runner == "update":
        result = apply_mod.run_update(
            **base, fetch=lambda: {"0.9.9": [{"yanked": False}]}, environ={}
        )
    elif runner == "repair":
        result = repair_mod.run_repair(**base, environ={})
    else:
        result = uninstall_mod.run_uninstall(**base, assume_yes=True)
    assert result.exit_code is ExitCode.REFUSED
    _assert_actionable(result.render_text())
    assert not (tmp_path / "lifecycle").exists()  # nothing journaled, nothing changed


def test_update_check_shows_the_resolver_reason(refused_report):
    result = REAL_CHECK(
        detect=lambda: refused_report,
        fetch=lambda: {"99.0.0": [{"yanked": False, "requires_python": ">=3.10"}]},
    )
    text = result.render_text()
    assert "could not verify its uv installation" in text
    _assert_actionable(text)


def test_a_deleted_cwd_reason_reaches_the_user_too(tmp_path):
    problem = uvpath.uv_problem_message(uvpath.CWD_UNAVAILABLE)
    report = own.evaluate_ownership(_uv_tool_evidence(problem))
    result = repair_mod.run_repair(**(_common(tmp_path) | {"detect": lambda: report}), environ={})
    text = result.render_text()
    assert result.exit_code is ExitCode.REFUSED
    assert "current directory could not be inspected" in text and "reinstall" not in text.lower()
