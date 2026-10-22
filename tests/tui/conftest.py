from __future__ import annotations

import os
import sys

import pytest

from .fake_backend import FakeBackend
from .harness import BACKEND_AVAILABLE, TermSession


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "tui" in item.path.parts:
            item.add_marker(pytest.mark.tui)
            if sys.platform != "win32" and not os.environ.get("VELUNE_TUI_POSIX"):
                item.add_marker(
                    pytest.mark.skip(
                        reason="POSIX PTY path unverified; set VELUNE_TUI_POSIX=1 to run"
                    )
                )
            elif not BACKEND_AVAILABLE:
                item.add_marker(pytest.mark.skip(reason="no PTY backend (pywinpty/ptyprocess)"))


@pytest.fixture(scope="session")
def backend():
    srv = FakeBackend().start()
    yield srv
    srv.stop()


@pytest.fixture
def launch(tmp_path, backend):
    """Factory: ``launch(rows=..., cols=..., argv=[...])`` -> started TermSession."""
    sessions: list[TermSession] = []

    def _launch(**kw) -> TermSession:
        work = tmp_path / "work"
        work.mkdir(exist_ok=True)
        s = TermSession(backend=backend, sandbox=tmp_path / "sandbox", workdir=work, **kw)
        sessions.append(s)
        return s

    yield _launch
    for s in sessions:
        s.close()
