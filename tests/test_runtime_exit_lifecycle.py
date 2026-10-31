"""Application exit and workspace switching agree on who owns the command sandbox.

Uses the real runtime (``build_runtime``) with a counting fake in place of the
sandbox, via the isolation fixtures of ``test_runtime_wiring``.
"""

from __future__ import annotations

from tests import test_runtime_wiring as wiring

SENTINEL_B = wiring.SENTINEL_B
_make_repo = wiring._make_repo
_switch = wiring._switch

# Re-exported by name so pytest finds them in this module.
isolated_home = wiring.isolated_home
network = wiring.network
runtime_a = wiring.runtime_a


class CountingSandbox:
    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1


def _executor(runtime):
    return runtime.container.get("runtime.execution_executor")


async def test_exit_stops_the_active_command_sandbox(runtime_a):
    sandbox = CountingSandbox()
    _executor(runtime_a).sandbox = sandbox

    await runtime_a.container.get("runtime.lifecycle").shutdown()

    assert sandbox.stopped == 1


async def test_exit_after_a_switch_stops_only_the_current_sandbox(runtime_a, tmp_path):
    old = CountingSandbox()
    _executor(runtime_a).sandbox = old
    await _switch(runtime_a, _make_repo(tmp_path / "ws_b", SENTINEL_B))
    assert old.stopped == 1  # stopped by the switch itself

    current = CountingSandbox()
    _executor(runtime_a).sandbox = current

    await runtime_a.container.get("runtime.lifecycle").shutdown()

    assert current.stopped == 1
    assert old.stopped == 1, "exit must not stop a superseded workspace's sandbox again"


async def test_a_b_a_then_exit_stops_each_sandbox_exactly_once(runtime_a, tmp_path):
    ws_a = tmp_path / "ws_a"
    ws_b = _make_repo(tmp_path / "ws_b", SENTINEL_B)
    superseded = []
    for target in (ws_b, ws_a):
        superseded.append(CountingSandbox())
        _executor(runtime_a).sandbox = superseded[-1]
        await _switch(runtime_a, target)
    last = CountingSandbox()
    _executor(runtime_a).sandbox = last

    await runtime_a.container.get("runtime.lifecycle").shutdown()

    assert [s.stopped for s in superseded] == [1, 1]
    assert last.stopped == 1


async def test_a_switch_stops_the_old_sandbox_off_the_event_loop(runtime_a, tmp_path):
    """``docker stop`` can take 10s+; running it on the loop would freeze the whole UI."""
    import asyncio
    import threading

    loop_thread = threading.current_thread()
    seen: list[threading.Thread] = []

    class SlowSandbox:
        def stop(self):
            seen.append(threading.current_thread())

    _executor(runtime_a).sandbox = SlowSandbox()
    await _switch(runtime_a, _make_repo(tmp_path / "ws_b", SENTINEL_B))
    await asyncio.sleep(0)

    assert len(seen) == 1
    assert seen[0] is not loop_thread, "sandbox.stop() ran on the event-loop thread"


async def test_a_switch_whose_rebuild_fails_does_not_stop_the_old_sandbox_twice(
    runtime_a, tmp_path, monkeypatch
):
    import velune.execution.subsystems as execution_subsystems

    old = CountingSandbox()
    _executor(runtime_a).sandbox = old

    def boom(env):
        raise RuntimeError("cannot build the executor")

    monkeypatch.setattr(execution_subsystems.EXECUTION_MODULES[0], "factory", boom, raising=False)
    await _switch(runtime_a, _make_repo(tmp_path / "ws_b", SENTINEL_B))
    assert runtime_a.container.get("runtime.execution_executor") is None

    await runtime_a.container.get("runtime.lifecycle").shutdown()

    assert old.stopped == 1
