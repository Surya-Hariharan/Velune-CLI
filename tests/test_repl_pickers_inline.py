"""Pickers reached from slash commands stay inside the REPL's Application.

A second prompt_toolkit Application started under the full-screen REPL draws
over the transcript and leaves the screen broken when it exits (``/project``
after ``/dashboard`` was the reported case). So with a host installed,
``single_select`` must always use it, whether or not the caller asked for the
palette look.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from velune.cli import interactive
from velune.cli.interactive import Option
from velune.cli.interactive import host as interactive_host


class _FakeHost:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def select(self, title, options, **_kwargs):
        self.calls.append(title)
        return options[-1].id


@pytest.fixture
def fake_host(monkeypatch):
    async def _no_standalone(*_a, **_k):
        raise AssertionError("a second Application was started inside the REPL")

    monkeypatch.setattr(interactive, "run_standalone", _no_standalone)
    h = _FakeHost()
    interactive_host.install(h)
    try:
        yield h
    finally:
        interactive_host.uninstall(h)


async def test_single_select_uses_the_host_without_palette_flag(fake_host):
    options = [Option(id="a", label="A"), Option(id="b", label="B")]
    assert await interactive.single_select("Project workspaces", options) == "b"
    assert fake_host.calls == ["Project workspaces"]


async def test_single_select_still_uses_the_host_with_palette_flag(fake_host):
    options = [Option(id="a", label="A")]
    assert await interactive.single_select("Pick", options, palette=True) == "a"
    assert fake_host.calls == ["Pick"]


def _status_repl(tmp_path: Path, jobs: list) -> SimpleNamespace:
    def _get(key: str):
        if key == "runtime.workspace":
            return str(tmp_path)
        raise KeyError(key)

    return SimpleNamespace(
        console=Console(file=io.StringIO(), width=100),
        container=SimpleNamespace(get=_get),
        _cached_branch=None,
        _project_profile=None,
        _load_project_profile=lambda: None,
        _mcp_registry=SimpleNamespace(status=lambda: []),
        _job_registry=SimpleNamespace(all_jobs=lambda: list(jobs)),
        _alert_store=None,
        active_model=None,
    )


async def test_project_status_lists_recent_jobs_by_name(tmp_path, monkeypatch):
    from velune.cli.handlers.workspace import _project_status
    from velune.core.task_registry import JobRecord, JobStatus

    monkeypatch.setattr("velune.providers.keystore.list_configured_providers", lambda: [])
    job = JobRecord(job_id="abc123", name="Refactor the parser")
    job.status = JobStatus.RUNNING
    repl = _status_repl(tmp_path, [job])

    await _project_status(repl)

    out = repl.console.file.getvalue()
    assert "Recent Tasks" in out
    assert "abc123" in out and "Refactor the parser" in out and "running" in out
    assert "Value" not in out  # no meaningless "Key / Value" header row
