"""`velune doctor` launcher checks read the interpreter a launcher really runs.

Regression: Console Launcher / Scripts on PATH assumed the `velune` launcher
sits next to `python.exe`. For the recommended isolated installs (the one-line
installer's `uv tool install`, and `pipx`) the launcher lives in a shared bin
dir while the interpreter lives in a private env, so a healthy install got two
warnings — one advising the user to put the private env's Scripts dir on PATH.
Launchers embed their interpreter path; the checks now read it.
"""

from __future__ import annotations

import shutil
import sys

import pytest

from velune.cli.commands import doctor
from velune.cli.commands.doctor import (
    _check_console_launcher,
    _check_scripts_on_path,
    _launcher_interpreter,
)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # Windows .exe launchers (pip/distlib and uv): binary stub, then the
        # shebang line in the payload. A decoy "#!" inside the stub is ignored.
        (
            b"MZ\x90\x00#!\x00\x00PATHEXT\x00stub...#!C:\\tools\\velune\\Scripts\\python.exe\r\nPK\x03\x04",
            "C:\\tools\\velune\\Scripts\\python.exe",
        ),
        (
            b'\x00\x00#!"C:\\Program Files\\Py 3\\python.exe"\nPK',
            "C:\\Program Files\\Py 3\\python.exe",
        ),
        # POSIX script launcher.
        (
            b"#!/home/u/.local/share/pipx/venvs/velune-cli/bin/python\nimport sys\n",
            "/home/u/.local/share/pipx/venvs/velune-cli/bin/python",
        ),
        # pip's long/space-path form.
        (
            b"#!/bin/sh\n'''exec' '/opt/my tools/venv/bin/python3.12' \"$0\" \"$@\"\n' '''\n",
            "/opt/my tools/venv/bin/python3.12",
        ),
        (b"#!/bin/sh\necho not a python launcher\n", None),
    ],
)
def test_launcher_interpreter_formats(tmp_path, payload, expected):
    launcher = tmp_path / "velune"
    launcher.write_bytes(payload)
    assert _launcher_interpreter(str(launcher)) == expected


def _launcher_on_path(monkeypatch, tmp_path, interpreter: str):
    bin_dir = tmp_path / "shared-bin"
    bin_dir.mkdir()
    launcher = bin_dir / "velune"
    launcher.write_bytes(b"#!" + interpreter.encode() + b"\n")
    monkeypatch.setattr(shutil, "which", lambda name: str(launcher) if name == "velune" else None)
    monkeypatch.setenv("PATH", str(bin_dir))  # the private env's Scripts dir is NOT on PATH
    return launcher


def test_isolated_install_layout_is_healthy(monkeypatch, tmp_path):
    # uv tool / pipx: launcher in a shared bin dir runs this very interpreter.
    _launcher_on_path(monkeypatch, tmp_path, sys.executable)
    assert _check_console_launcher()["status"] == "ok"
    assert _check_scripts_on_path()["status"] == "ok"


def test_launcher_for_another_install_still_warns(monkeypatch, tmp_path):
    other = str(tmp_path / "other-env" / "bin" / "python")
    _launcher_on_path(monkeypatch, tmp_path, other)
    result = _check_console_launcher()
    assert result["status"] == "warn"
    assert other in result["message"]
    assert _check_scripts_on_path()["status"] == "warn"


def test_no_launcher_on_path_warns(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    assert _check_console_launcher()["status"] == "warn"
