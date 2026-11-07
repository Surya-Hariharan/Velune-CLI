"""Upgrade/reinstall hints must fit how Velune was installed (P2.2 §G).

A uv tool environment has no pip, so the old "python -m pip install --upgrade"
advice cannot work there. Non-uv installs keep their existing wording.
"""

from __future__ import annotations

import importlib.metadata as md

import pytest

from velune.cli.commands.doctor import _check_core_dependencies
from velune.lifecycle import hints
from velune.main import _fatal_environment_error


def test_receipt_marks_a_uv_tool_environment(tmp_path):
    assert not hints.is_uv_tool_environment(str(tmp_path))
    (tmp_path / "uv-receipt.toml").write_text("[tool]\n")
    assert hints.is_uv_tool_environment(str(tmp_path))


def test_a_receipt_directory_is_not_a_receipt(tmp_path):
    (tmp_path / "uv-receipt.toml").mkdir()
    assert not hints.is_uv_tool_environment(str(tmp_path))


def test_advice_text():
    assert "uv tool install --force velune-cli" in hints.uv_reinstall_advice("linux")
    assert "fresh terminal" not in hints.uv_reinstall_advice("linux")
    assert "Close Velune first" in hints.uv_reinstall_advice("win32")
    assert "pip" not in hints.uv_reinstall_advice("win32")


def _broken_deps(monkeypatch):
    monkeypatch.setattr(md, "requires", lambda dist: ["typer>=0.16.0", "rich>=13.9.0"])

    def version(name):
        if name == "rich":
            raise md.PackageNotFoundError(name)
        return "0.27.0"

    monkeypatch.setattr(md, "version", version)


def test_doctor_names_uv_for_uv_managed_installs(monkeypatch):
    _broken_deps(monkeypatch)
    monkeypatch.setattr(hints, "is_uv_tool_environment", lambda prefix=None: True)
    message = _check_core_dependencies()["message"]
    assert "missing: rich" in message
    assert "uv tool install --force velune-cli" in message
    assert "pip install" not in message


def test_doctor_keeps_the_pip_advice_for_other_installs(monkeypatch):
    _broken_deps(monkeypatch)
    monkeypatch.setattr(hints, "is_uv_tool_environment", lambda prefix=None: False)
    message = _check_core_dependencies()["message"]
    assert "pip install --upgrade velune-cli" in message
    assert "pipx install velune-cli" in message
    assert "uv tool install --force" not in message


def test_startup_failure_names_uv_for_uv_managed_installs(monkeypatch, capsys):
    monkeypatch.setattr(hints, "is_uv_tool_environment", lambda prefix=None: True)
    with pytest.raises(SystemExit):
        _fatal_environment_error(ImportError("cannot import name 'Foo' from 'typer.main' (x)"))
    err = capsys.readouterr().err
    assert "'typer'" in err and "uv tool install --force velune-cli" in err
    assert "pip install --upgrade" not in err and "pipx" not in err


def test_startup_failure_keeps_pip_advice_for_other_installs(monkeypatch, capsys):
    monkeypatch.setattr(hints, "is_uv_tool_environment", lambda prefix=None: False)
    with pytest.raises(SystemExit):
        _fatal_environment_error(ImportError("cannot import name 'Foo' from 'typer.main' (x)"))
    err = capsys.readouterr().err
    assert "pip install --upgrade velune-cli" in err and "pipx install velune-cli" in err


def test_startup_failure_survives_a_broken_hint_helper(monkeypatch, capsys):
    def boom(prefix=None):
        raise RuntimeError("hint helper broke")

    monkeypatch.setattr(hints, "is_uv_tool_environment", boom)
    with pytest.raises(SystemExit):
        _fatal_environment_error(ImportError("cannot import name 'Foo' from 'typer.main' (x)"))
    assert "pip install --upgrade velune-cli" in capsys.readouterr().err


def test_interpreter_failures_are_unchanged(capsys):
    with pytest.raises(SystemExit):
        _fatal_environment_error(
            ImportError("DLL load failed while importing _ctypes", name="_ctypes")
        )
    assert "Reinstall Python" in capsys.readouterr().err
