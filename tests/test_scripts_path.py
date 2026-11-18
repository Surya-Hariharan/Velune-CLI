"""Scripts-directory discovery: the launcher exists, but is it on PATH? Report only, never mutate."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from velune.cli.commands import doctor
from velune.lifecycle import scripts_path as sp

USER_SCRIPTS_WIN = r"C:\Users\me\AppData\Roaming\Python\Python315\Scripts"


def _make_launcher(tmp_path: Path, platform: str) -> Path:
    scripts = tmp_path / "Python" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / sp.launcher_name(platform)).write_bytes(b"MZ-fake")
    return scripts


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_launcher_name_per_platform(platform):
    assert sp.launcher_name(platform) == ("velune.exe" if platform == "win32" else "velune")


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_missing_scripts_path_is_reported_with_advice(tmp_path, platform):
    scripts = _make_launcher(tmp_path, platform)
    report = sp.diagnose(dirs=[scripts], environ={"PATH": "/usr/bin"}, platform=platform)
    assert report.launcher_present and not report.on_path
    assert report.scripts_dir == scripts
    assert str(scripts) in report.advice
    assert "python -m velune" in report.advice


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_existing_scripts_path_is_ok(tmp_path, platform):
    if platform != "win32" and os.name == "nt":
        pytest.skip("a drive-letter tmp path cannot sit in a ':'-separated PATH; runs on CI")
    scripts = _make_launcher(tmp_path, platform)
    sep = ";" if platform == "win32" else ":"
    env = {"PATH": sep.join(["/x", str(scripts)])}
    report = sp.diagnose(dirs=[scripts], environ=env, platform=platform)
    assert report.launcher_present and report.on_path and report.advice is None


def test_windows_path_match_is_case_and_slash_insensitive():
    env = {"PATH": r"c:\users\ME\appdata\roaming\python\python315\scripts\;C:\other"}
    assert sp.dir_on_path(USER_SCRIPTS_WIN, env, "win32")
    assert not sp.dir_on_path(USER_SCRIPTS_WIN, {"PATH": r"C:\other"}, "win32")


def test_posix_path_match_is_case_sensitive():
    assert not sp.dir_on_path("/Home/Me/bin", {"PATH": "/home/me/bin"}, "linux")
    assert sp.dir_on_path("/home/me/bin", {"PATH": "/usr/bin:/home/me/bin/"}, "linux")


def test_no_launcher_means_no_install_claim(tmp_path):
    empty = tmp_path / "Scripts"
    empty.mkdir()
    report = sp.diagnose(dirs=[empty], environ={"PATH": ""}, platform="win32")
    assert not report.launcher_present and report.advice is None


def test_windows_advice_is_powershell_user_scope_and_quotes_apostrophes():
    advice = sp.path_fix_advice(r"C:\Users\O'Neil\Scripts", "win32")
    assert "SetEnvironmentVariable('Path'" in advice and "'User')" in advice
    assert "O''Neil" in advice and "NEW" in advice


def test_diagnosis_never_touches_environment_or_starts_processes(tmp_path, monkeypatch):
    scripts = _make_launcher(tmp_path, "win32")
    before = dict(os.environ)

    def boom(*a, **k):
        raise AssertionError("diagnosis must not start processes")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    sp.diagnose(dirs=[scripts], environ={"PATH": ""}, platform="win32")
    assert dict(os.environ) == before


def test_candidates_come_from_sysconfig_not_hardcoded_versions():
    dirs = sp.candidate_scripts_dirs()
    assert dirs
    assert all(d.is_absolute() for d in dirs)
    import sysconfig

    assert Path(sysconfig.get_path("scripts")) in dirs


def test_doctor_reports_installed_but_not_on_path_as_warning_not_failure(tmp_path, monkeypatch):
    scripts = _make_launcher(tmp_path, sys.platform)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(sp, "candidate_scripts_dirs", lambda: [scripts])
    monkeypatch.setenv("PATH", str(tmp_path / "elsewhere"))
    result = doctor._check_scripts_on_path()
    assert result["status"] == "warn"  # install succeeded: never "fail"
    assert str(scripts) in result["message"]
    assert "not recognized" in result["message"]


def test_doctor_ok_when_scripts_dir_is_on_path(tmp_path, monkeypatch):
    scripts = _make_launcher(tmp_path, sys.platform)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(sp, "candidate_scripts_dirs", lambda: [scripts])
    monkeypatch.setenv("PATH", os.pathsep.join(["/x", str(scripts)]))
    assert doctor._check_scripts_on_path()["status"] == "ok"


def test_direct_executable_invocation_reports_version():
    """Real-environment check: the installed entry point runs without PATH help."""
    launcher = sp.find_launcher_dir(sp.candidate_scripts_dirs())
    if launcher is None:
        pytest.skip("velune console launcher is not installed in this environment")
    exe = launcher / sp.launcher_name()
    out = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.startswith("velune v")


def test_add_to_user_path_non_windows_only_advises(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    changed, msg = sp.add_to_user_path("/home/me/.local/bin")
    assert changed is False
    assert "/home/me/.local/bin" in msg


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry only")
def test_add_to_user_path_windows_appends_once(monkeypatch):
    import winreg

    store = {"Path": (r"C:\Existing", winreg.REG_EXPAND_SZ)}

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(winreg, "OpenKey", lambda *a, **k: _Key())
    monkeypatch.setattr(winreg, "QueryValueEx", lambda key, name: store[name])
    monkeypatch.setattr(
        winreg, "SetValueEx", lambda key, name, _r, kind, val: store.__setitem__(name, (val, kind))
    )
    changed, _ = sp.add_to_user_path(USER_SCRIPTS_WIN)
    assert changed and store["Path"][0] == rf"C:\Existing;{USER_SCRIPTS_WIN}"
    assert store["Path"][1] == winreg.REG_EXPAND_SZ
    changed, _ = sp.add_to_user_path(USER_SCRIPTS_WIN)
    assert changed is False
