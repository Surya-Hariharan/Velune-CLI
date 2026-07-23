"""Ctrl+C must actually kill a running shell subprocess, not just the
awaiting coroutine.

`asyncio.Task.cancel()` cannot interrupt work already running inside
`asyncio.to_thread` — the `CancelledError` is only delivered once the thread
function returns, so a shell command kept running silently past a Ctrl+C
until its own timeout. Fixed with a plain `threading.Event` threaded into
`SubprocessSandbox.execute()`'s existing poll loop
(`velune/execution/cancellation.py`), set directly by
`InterruptController.cancel_foreground()` instead of relying on asyncio
cancellation to reach the worker thread.
"""

from __future__ import annotations

import threading
import time

import psutil
import pytest

from velune.core.errors.execution import SandboxError
from velune.execution import cancellation
from velune.execution.command_spec import CommandSpec
from velune.execution.sandbox import SubprocessSandbox


def _long_running_spec(tmp_path):
    # `python -c` is blocked entirely (inline-code execution guard — bypasses
    # the workspace boundary) — write a real script file instead, exactly
    # what the guard's own error message suggests.
    script = tmp_path / "sleep_long.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    return CommandSpec.from_string("python sleep_long.py", cwd=tmp_path, timeout=60.0)


# ── cancellation registry ────────────────────────────────────────────────


def test_register_unregister_roundtrip():
    event = threading.Event()
    cancellation.register(event)
    cancellation.unregister(event)
    cancellation.cancel_all()  # must not raise, and must not set a stale event
    assert not event.is_set()


def test_cancel_all_sets_every_registered_event():
    a, b = threading.Event(), threading.Event()
    cancellation.register(a)
    cancellation.register(b)
    try:
        cancellation.cancel_all()
        assert a.is_set()
        assert b.is_set()
    finally:
        cancellation.unregister(a)
        cancellation.unregister(b)


def test_cancel_all_ignores_unregistered_events():
    event = threading.Event()
    cancellation.cancel_all()
    assert not event.is_set()


# ── sandbox-level cancellation ───────────────────────────────────────────


@pytest.mark.timeout(20)
def test_cancel_event_kills_a_running_process(tmp_path):
    sandbox = SubprocessSandbox(tmp_path)
    spec = _long_running_spec(tmp_path)
    cancel_event = threading.Event()

    result_holder: dict = {}

    def _run():
        try:
            sandbox.execute(spec, cancel_event)
        except SandboxError as exc:
            result_holder["error"] = exc

    thread = threading.Thread(target=_run)
    thread.start()

    # Give the child process a moment to actually spawn, then cancel — well
    # before the 30s sleep or the 60s spec timeout would otherwise elapse.
    time.sleep(0.5)
    started = time.perf_counter()
    cancel_event.set()
    thread.join(timeout=10)
    elapsed = time.perf_counter() - started

    assert not thread.is_alive(), "sandbox.execute did not return after cancellation"
    assert elapsed < 10, "cancellation should abort well before the 30s sleep completes"
    assert "error" in result_holder
    assert "cancelled" in str(result_holder["error"]).lower()


@pytest.mark.timeout(20)
def test_cancel_event_actually_terminates_the_os_process(tmp_path):
    """Not just that execute() returns — the child process must be gone."""
    sandbox = SubprocessSandbox(tmp_path)
    spec = _long_running_spec(tmp_path)
    cancel_event = threading.Event()
    pids: list[int] = []

    import velune.execution.sandbox as sandbox_mod

    original_popen = sandbox_mod.subprocess.Popen

    def _capturing_popen(*args, **kwargs):
        proc = original_popen(*args, **kwargs)
        pids.append(proc.pid)
        return proc

    sandbox_mod.subprocess.Popen = _capturing_popen
    try:

        def _run():
            try:
                sandbox.execute(spec, cancel_event)
            except SandboxError:
                pass

        thread = threading.Thread(target=_run)
        thread.start()
        time.sleep(0.5)
        cancel_event.set()
        thread.join(timeout=10)
    finally:
        sandbox_mod.subprocess.Popen = original_popen

    assert pids, "no process was captured"
    # Give the OS a brief moment to reap the killed process.
    time.sleep(0.3)
    assert not psutil.pid_exists(pids[0]), "the child process is still running after cancellation"


@pytest.mark.timeout(20)
def test_no_cancel_event_behaves_exactly_as_before(tmp_path):
    """cancel_event is optional — omitting it must not change existing behavior."""
    sandbox = SubprocessSandbox(tmp_path)
    spec = CommandSpec.from_string("echo hello", cwd=tmp_path, timeout=10.0)

    result = sandbox.execute(spec)

    assert result.exit_code == 0
    assert "hello" in result.stdout


@pytest.mark.timeout(20)
async def test_execute_command_registers_and_unregisters_its_cancel_event(tmp_path, monkeypatch):
    """ExecuteCommand must clean up its cancel_event even on success, so a
    long REPL session doesn't leak events into the registry forever."""
    from velune.tools.safety import ApprovalMode
    from velune.tools.terminal.execute import ExecuteCommand

    tool = ExecuteCommand(workspace_path=str(tmp_path), approval_mode=ApprovalMode.SAFE)

    seen_during: list[int] = []
    original_register = cancellation.register

    def _spying_register(event):
        original_register(event)
        seen_during.append(1)

    monkeypatch.setattr(cancellation, "register", _spying_register)

    result = await tool.execute("echo hi", directory=str(tmp_path))

    assert result["exit_code"] == 0
    assert seen_during, "execute() never registered a cancel event"
    # Nothing should remain registered once the call has completed.
    with cancellation._lock:
        assert len(cancellation._active) == 0
