"""Startup import-failure messages must point at the real cause.

A stale/conflicting third-party dependency and a genuinely broken Python
both surface as an ImportError while importing the CLI. Telling a user with
an old `typer` to "reinstall Python" sends them down the wrong path, so the
two cases get different, actionable messages.
"""

from __future__ import annotations

import pytest

from velune.main import _failing_dependency, _fatal_environment_error


def test_cannot_import_name_from_third_party_is_attributed_to_that_dist():
    exc = ImportError("cannot import name 'Foo' from 'rich.console' (/x/rich/console.py)")
    assert _failing_dependency(exc) == "rich"


def test_missing_third_party_module_maps_import_name_to_distribution():
    # import name `prompt_toolkit` → distribution `prompt_toolkit`/`prompt-toolkit`
    exc = ModuleNotFoundError("No module named 'prompt_toolkit'", name="prompt_toolkit")
    assert _failing_dependency(exc).replace("-", "_").lower() == "prompt_toolkit"


@pytest.mark.parametrize(
    "exc",
    [
        ImportError("DLL load failed while importing _ctypes", name="_ctypes"),
        ModuleNotFoundError("No module named 'sqlite3'", name="sqlite3"),
        ModuleNotFoundError("No module named 'velune.cli.app'", name="velune.cli.app"),
        ImportError("something unattributable"),
    ],
)
def test_interpreter_level_failures_are_not_blamed_on_a_dependency(exc):
    assert _failing_dependency(exc) is None


def test_dependency_failure_message_says_upgrade_not_reinstall_python(capsys):
    exc = ImportError("cannot import name 'Foo' from 'typer.main' (/x/typer/main.py)")
    with pytest.raises(SystemExit):
        _fatal_environment_error(exc)
    err = capsys.readouterr().err
    assert "'typer'" in err
    assert "pip install --upgrade velune-cli" in err
    assert "pipx install velune-cli" in err
    assert "Reinstall Python" not in err


def test_interpreter_failure_message_still_says_reinstall_python(capsys):
    exc = ImportError("DLL load failed while importing _ctypes", name="_ctypes")
    with pytest.raises(SystemExit):
        _fatal_environment_error(exc)
    assert "Reinstall Python" in capsys.readouterr().err
