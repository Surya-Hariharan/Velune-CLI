"""Owned resources are released at the end of their lifecycle (Phase 3A).

Fakes only: no Docker, no real shell, no network. The command sandbox used to be
stopped on a workspace switch but never at application exit, because
``ExecutionExecutor`` had no ``shutdown()`` for ``LifecycleCoordinator`` to call.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest

from velune.execution import cancellation
from velune.execution.executor import ExecutionExecutor
from velune.kernel.lifecycle import LifecycleCoordinator


class FakeSandbox:
    def __init__(self, *, fail: Exception | None = None, block: float = 0.0):
        self.stopped = 0
        self._fail = fail
        self._block = block
        self.release = threading.Event()

    def stop(self):
        self.stopped += 1
        if self._block:
            self.release.wait(self._block)
        if self._fail:
            raise self._fail


def _executor(tmp_path, sandbox) -> ExecutionExecutor:
    executor = ExecutionExecutor(tmp_path)  # default subprocess sandbox; never executes anything
    executor.sandbox = sandbox
    return executor


# ── ExecutionExecutor.shutdown ────────────────────────────────────────────────


async def test_shutdown_stops_the_sandbox(tmp_path):
    sandbox = FakeSandbox()
    await _executor(tmp_path, sandbox).shutdown()
    assert sandbox.stopped == 1


async def test_repeated_shutdown_stops_the_sandbox_once(tmp_path):
    sandbox = FakeSandbox()
    executor = _executor(tmp_path, sandbox)
    await executor.shutdown()
    await executor.shutdown()
    await asyncio.gather(executor.shutdown(), executor.shutdown())
    assert sandbox.stopped == 1


async def test_shutdown_tolerates_a_sandbox_without_stop(tmp_path):
    executor = ExecutionExecutor(tmp_path)  # SubprocessSandbox has no stop()
    await executor.shutdown()
    await executor.shutdown()


async def test_a_failing_stop_does_not_raise(tmp_path):
    sandbox = FakeSandbox(fail=RuntimeError("daemon gone"))
    await _executor(tmp_path, sandbox).shutdown()
    assert sandbox.stopped == 1


async def test_a_wedged_stop_cannot_block_exit_indefinitely(tmp_path):
    sandbox = FakeSandbox(block=5.0)
    executor = _executor(tmp_path, sandbox)
    started = time.monotonic()
    await executor.shutdown(timeout=0.2)
    assert time.monotonic() - started < 2.0
    sandbox.release.set()  # let the worker thread finish


async def test_cancellation_mid_stop_still_completes_the_stop(tmp_path):
    sandbox = FakeSandbox(block=5.0)
    executor = _executor(tmp_path, sandbox)
    task = asyncio.create_task(executor.shutdown())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    sandbox.release.set()
    await asyncio.sleep(0.1)
    assert sandbox.stopped == 1


def test_a_failed_construction_does_not_leak_the_sandbox(tmp_path, monkeypatch):
    import velune.execution.executor as mod

    sandbox = FakeSandbox()
    monkeypatch.setattr(mod, "_build_sandbox", lambda *a, **k: sandbox)

    def boom(*a, **k):
        raise RuntimeError("validator failed")

    monkeypatch.setattr(mod, "PostExecutionValidator", boom)
    with pytest.raises(RuntimeError, match="validator failed"):
        ExecutionExecutor(tmp_path)
    assert sandbox.stopped == 1


async def test_sandbox_init_failure_falls_back_and_exits_cleanly(tmp_path, monkeypatch):
    """Docker requested but unavailable: the executor falls back; exit is a no-op."""
    import velune.execution.docker_sandbox as docker_mod

    class Cfg:
        class execution:  # noqa: N801
            docker_sandbox = True
            docker_image = "x"
            allowed_executables = None

    class Boom:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise docker_mod.DockerUnavailableError("no docker")

    monkeypatch.setattr(docker_mod, "DockerSandbox", Boom)
    executor = ExecutionExecutor(tmp_path, config=Cfg)
    assert not isinstance(executor.sandbox, Boom)
    await executor.shutdown()


# ── LifecycleCoordinator ──────────────────────────────────────────────────────


async def test_lifecycle_exit_stops_the_registered_executors_sandbox(tmp_path):
    sandbox = FakeSandbox()
    lifecycle = LifecycleCoordinator()
    lifecycle.register("execution", _executor(tmp_path, sandbox))
    await lifecycle.startup()
    await lifecycle.shutdown()
    assert sandbox.stopped == 1


async def test_exit_after_command_execution_still_stops_the_sandbox(tmp_path):
    sandbox = FakeSandbox()
    sandbox.execute = lambda spec, cancel=None: None  # a command ran earlier
    sandbox.execute(None)
    lifecycle = LifecycleCoordinator()
    lifecycle.register("execution", _executor(tmp_path, sandbox))
    await lifecycle.shutdown()
    assert sandbox.stopped == 1


class _Recorder:
    def __init__(self, name, log, *, exc: BaseException | None = None):
        self.name, self.log, self.exc = name, log, exc

    async def shutdown(self):
        self.log.append(self.name)
        if self.exc is not None:
            raise self.exc


async def test_a_failing_component_does_not_prevent_the_others(tmp_path):
    log: list[str] = []
    sandbox = FakeSandbox()
    lifecycle = LifecycleCoordinator()
    lifecycle.register("execution", _executor(tmp_path, sandbox))
    lifecycle.register("a", _Recorder("a", log))
    lifecycle.register("bad", _Recorder("bad", log, exc=RuntimeError("boom")))
    lifecycle.register("z", _Recorder("z", log))

    await lifecycle.shutdown()

    assert log == ["z", "bad", "a"]
    assert sandbox.stopped == 1


async def test_cancellation_during_shutdown_still_runs_remaining_cleanup(tmp_path):
    log: list[str] = []
    sandbox = FakeSandbox()
    lifecycle = LifecycleCoordinator()
    lifecycle.register("execution", _executor(tmp_path, sandbox))
    lifecycle.register("a", _Recorder("a", log))
    lifecycle.register("cancelled", _Recorder("cancelled", log, exc=asyncio.CancelledError()))
    lifecycle.register("z", _Recorder("z", log))

    with pytest.raises(asyncio.CancelledError):
        await lifecycle.shutdown()

    assert log == ["z", "cancelled", "a"]
    assert sandbox.stopped == 1


async def test_repeated_lifecycle_shutdown_is_safe(tmp_path):
    sandbox = FakeSandbox()
    lifecycle = LifecycleCoordinator()
    lifecycle.register("execution", _executor(tmp_path, sandbox))
    await lifecycle.shutdown()
    await lifecycle.shutdown()
    assert sandbox.stopped == 1


# ── running commands at exit ──────────────────────────────────────────────────


async def test_exit_trips_the_cancel_event_of_a_command_running_on_a_worker_thread():
    """Task cancellation cannot interrupt ``to_thread``; the event is what kills the tree."""
    event = threading.Event()
    cancellation.register(event)
    try:
        worker = asyncio.create_task(asyncio.to_thread(event.wait, 10.0))
        await asyncio.sleep(0.05)
        started = time.monotonic()

        await LifecycleCoordinator().shutdown()

        assert event.is_set()
        assert await asyncio.wait_for(worker, timeout=2.0) is True
        assert time.monotonic() - started < 2.0
    finally:
        cancellation.unregister(event)


# ── SQLiteManager write path ──────────────────────────────────────────────────


async def test_sqlite_manager_write_closes_its_connection_when_the_statement_fails(
    tmp_path, monkeypatch
):
    from velune.memory.storage import sqlite_manager as sm

    opened: list[sqlite3.Connection] = []
    real = sqlite3.connect

    def spy(*a, **k):
        conn = real(*a, **k)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sm.sqlite3, "connect", spy)
    manager = sm.SQLiteManager(tmp_path / "m.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            manager._do_write("INSERT INTO missing_table VALUES (1)", ())
    finally:
        await manager.shutdown()

    assert opened
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
