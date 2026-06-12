"""Shared fixtures for the Velune test suite."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True, scope="session")
def _interpreter_on_path() -> None:
    """Ensure the running interpreter's directory is on PATH.

    Sandbox tests resolve the python executable via ``shutil.which`` by
    basename; in some environments (e.g. a venv invoked by absolute path)
    the interpreter's directory is not on PATH at all.
    """
    exe_dir = str(Path(sys.executable).parent)
    if exe_dir not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = exe_dir + os.pathsep + os.environ.get("PATH", "")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """An isolated workspace root on the real filesystem."""
    root = tmp_path / "workspace"
    root.mkdir()
    return root
