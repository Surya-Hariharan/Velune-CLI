"""The council stream must always terminate: a stuck repo scan or a crash can't hang it."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from velune.cognition import orchestrator as orch_mod
from velune.cognition.orchestrator import CouncilOrchestrator


class _Container:
    def __init__(self, repo):
        self._repo = repo

    def has(self, key):
        return key == "runtime.repository_cognition" and self._repo is not None

    def get(self, key):
        return self._repo


class _HangingRepo:
    async def probe_for_changes(self):
        await asyncio.sleep(3600)

    async def get_snapshot_fresh(self):
        await asyncio.sleep(3600)


def _orchestrator(repo, execute):
    orch = CouncilOrchestrator.__new__(CouncilOrchestrator)
    orch._states = {}
    orch._runtime_container = lambda: _Container(repo)
    orch.firewall = SimpleNamespace()
    orch.max_wall_time_seconds = 5.0
    orch.execute_task = execute
    return orch


async def _drain(orch, timeout=5.0):
    async def run():
        return [item async for item in orch.stream("do a thing")]

    return await asyncio.wait_for(run(), timeout=timeout)


async def _ok(**kw):
    return {"final_summary": "done", "coder_proposal": None}


async def test_a_hung_repository_scan_does_not_hang_the_run(monkeypatch):
    monkeypatch.setattr(orch_mod, "_CONTEXT_TIMEOUT_S", 0.05)
    orch = _orchestrator(_HangingRepo(), _ok)
    items = await _drain(orch)
    assert any("Gathering repository context" in i.message for i in items)
    (state,) = orch._states.values()
    assert state.output == "done"  # the run proceeded without repo context


async def test_stream_ends_when_the_run_raises():
    async def boom(**kw):
        raise RuntimeError("provider exploded")

    orch = _orchestrator(None, boom)
    await _drain(orch)
    (state,) = orch._states.values()
    assert "provider exploded" in state.error


async def test_stream_ends_when_building_the_final_state_fails(monkeypatch):
    """Regression: an error after the try block skipped the sentinel and hung the consumer."""
    orch = _orchestrator(None, _ok)

    class Exploding(dict):
        def __setitem__(self, key, value):
            raise RuntimeError("state store failed")

    orch._states = Exploding()
    await _drain(orch)  # must return, not hang


async def test_cancelled_run_still_terminates():
    started = asyncio.Event()

    async def slow(**kw):
        started.set()
        await asyncio.sleep(3600)

    orch = _orchestrator(None, slow)

    async def consume():
        async for _ in orch.stream("x"):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
