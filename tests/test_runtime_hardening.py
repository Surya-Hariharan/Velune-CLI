"""Phase 3B: shutdown contract, real process-tree termination, Docker sandbox contract.

Evidence is labelled by kind:

* **real processes** -- `RealProcessTree` tests spawn disposable Python processes (a
  parent plus a grandchild) and check them with ``psutil``. Nothing outside the test
  is ever signalled.
* **fake docker-py** -- `DockerSandbox` is driven against a fake client. This proves
  the stop contract, not that a real daemon removes a real container.
* **real Docker** -- `test_real_docker_*` only runs when a daemon is reachable and the
  image is already present locally (no pull, no network); otherwise it is skipped.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from velune.core.errors.execution import SandboxError
from velune.execution import cancellation
from velune.execution import sandbox as sandbox_mod
from velune.execution.command_spec import CommandSpec
from velune.execution.executor import ExecutionExecutor
from velune.execution.sandbox import SubprocessSandbox
from velune.kernel.lifecycle import LifecycleCoordinator

# ── real process trees ────────────────────────────────────────────────────────

TREE_SCRIPT = """\
import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(120)
"""


@pytest.fixture
def pinned_python(monkeypatch):
    """Pin `python` to this interpreter; host PATH trust is not what is under test."""

    def pin(self):
        object.__setattr__(self, "_validated_exe", sys.executable)

    monkeypatch.setattr(CommandSpec, "_validate_host_executable", pin)


class RealProcessTree:
    """A command that is a real parent process with a real grandchild."""

    def __init__(self, workspace: Path, timeout: float = 60.0):
        self.pid_file = workspace / "child.pid"
        (workspace / "tree.py").write_text(TREE_SCRIPT, encoding="utf-8")
        self.sandbox = SubprocessSandbox(workspace, allowed_executables=["python"])
        self.spec = CommandSpec("python", ("tree.py", "child.pid"), workspace, timeout)
        self.event = threading.Event()
        self.worker: asyncio.Task | None = None
        self.parent_pid: int | None = None
        self.child_pid: int | None = None
        self._procs: list[psutil.Process] = []

    async def start(self) -> None:
        cancellation.register(self.event)
        real_popen = sandbox_mod.subprocess.Popen
        spawned: list[int] = []

        def spy(*a, **k):
            proc = real_popen(*a, **k)
            spawned.append(proc.pid)
            return proc

        sandbox_mod.subprocess.Popen = spy  # restored in cleanup()
        self._real_popen = real_popen
        self.worker = asyncio.create_task(
            asyncio.to_thread(self.sandbox.execute, self.spec, self.event)
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.pid_file.exists() and self.pid_file.read_text().strip() and spawned:
                break
            await asyncio.sleep(0.05)
        else:  # pragma: no cover - environment failure
            raise AssertionError("the test process tree never started")
        self.parent_pid = spawned[0]
        self.child_pid = int(self.pid_file.read_text())
        self._procs = [psutil.Process(self.parent_pid), psutil.Process(self.child_pid)]
        assert all(p.is_running() for p in self._procs)

    def alive(self) -> list[int]:
        return [p.pid for p in self._procs if p.is_running()]

    def cleanup(self) -> None:
        """Last resort: kill only the processes this test itself created."""
        sandbox_mod.subprocess.Popen = self._real_popen
        cancellation.unregister(self.event)
        for proc in self._procs:
            try:
                proc.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(self._procs, timeout=5)


@pytest.fixture
async def tree(tmp_path, pinned_python):
    t = RealProcessTree(tmp_path)
    yield t
    t.cleanup()


async def test_shutdown_terminates_a_running_command_and_its_children(tree):
    await tree.start()

    started = time.monotonic()
    await LifecycleCoordinator().shutdown()

    with pytest.raises(SandboxError, match="cancelled"):
        await asyncio.wait_for(tree.worker, timeout=15)
    assert time.monotonic() - started < 15
    assert tree.event.is_set()
    psutil.wait_procs(tree._procs, timeout=5)
    assert tree.alive() == [], "the command's process tree outlived shutdown"


async def test_repeated_shutdown_with_a_running_command_is_safe(tree):
    await tree.start()
    lifecycle = LifecycleCoordinator()

    await lifecycle.shutdown()
    await lifecycle.shutdown()

    with pytest.raises(SandboxError, match="cancelled"):
        await asyncio.wait_for(tree.worker, timeout=15)
    psutil.wait_procs(tree._procs, timeout=5)
    assert tree.alive() == []


async def test_a_failure_while_walking_the_process_tree_still_kills_the_command(tree, monkeypatch):
    """Enumerating children can fail (AccessDenied, races); the parent must still die."""
    await tree.start()

    def denied(self, *a, **k):
        raise psutil.AccessDenied(self.pid)

    monkeypatch.setattr(psutil.Process, "children", denied)

    await LifecycleCoordinator().shutdown()

    with pytest.raises(SandboxError, match="cancelled"):
        await asyncio.wait_for(tree.worker, timeout=15)
    parent = tree._procs[0]
    psutil.wait_procs([parent], timeout=5)
    assert not parent.is_running()


async def test_a_timed_out_command_tree_is_killed_too(tmp_path, pinned_python):
    t = RealProcessTree(tmp_path, timeout=1.0)
    try:
        await t.start()
        with pytest.raises(SandboxError, match="timed out"):
            await asyncio.wait_for(t.worker, timeout=15)
        psutil.wait_procs(t._procs, timeout=5)
        assert t.alive() == []
    finally:
        t.cleanup()


# ── shutdown contract ─────────────────────────────────────────────────────────


class Recorder:
    def __init__(self, name, log, *, hang: bool = False, delay: float = 0.0, exc=None):
        self.name, self.log, self.hang, self.delay, self.exc = name, log, hang, delay, exc

    async def shutdown(self):
        self.log.append(self.name)
        if self.hang:
            await asyncio.sleep(3600)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc


async def test_a_hanging_component_cannot_block_shutdown_forever():
    log: list[str] = []
    lifecycle = LifecycleCoordinator()
    lifecycle.COMPONENT_SHUTDOWN_TIMEOUT_S = 0.2
    lifecycle.register("a", Recorder("a", log))
    lifecycle.register("hung", Recorder("hung", log, hang=True))
    lifecycle.register("z", Recorder("z", log))

    started = time.monotonic()
    await asyncio.wait_for(lifecycle.shutdown(), timeout=5)

    assert time.monotonic() - started < 3
    assert log == ["z", "hung", "a"]
    assert lifecycle._components == {} and lifecycle._states == {}


def test_the_component_timeout_outlasts_the_executors_sandbox_stop():
    """A legitimate sandbox stop must not be cut short by the coordinator."""
    from velune.execution.executor import SANDBOX_STOP_TIMEOUT_S

    assert LifecycleCoordinator.COMPONENT_SHUTDOWN_TIMEOUT_S > SANDBOX_STOP_TIMEOUT_S


async def test_repeated_cancellation_still_reaches_every_component_and_is_reraised():
    log: list[str] = []
    lifecycle = LifecycleCoordinator()
    lifecycle.register("first", Recorder("first", log))
    lifecycle.register("second", Recorder("second", log, delay=1.0))
    lifecycle.register("third", Recorder("third", log, delay=1.0))
    lifecycle.register("fourth", Recorder("fourth", log))

    task = asyncio.create_task(lifecycle.shutdown())
    await asyncio.sleep(0.2)  # inside "fourth"? no: reverse order -> fourth, third, ...
    task.cancel()
    await asyncio.sleep(0.2)
    task.cancel()  # a second cancel while another component is still shutting down
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)

    assert log == ["fourth", "third", "second", "first"]
    assert lifecycle._components == {} and lifecycle._states == {}


async def test_shutdown_runs_fully_when_the_calling_task_is_already_cancelled():
    """`_async_main` calls shutdown from a ``finally`` while it is being cancelled."""
    log: list[str] = []
    lifecycle = LifecycleCoordinator()
    lifecycle.register("a", Recorder("a", log, delay=0.05))
    lifecycle.register("b", Recorder("b", log, delay=0.05))

    async def main():
        try:
            await asyncio.sleep(60)
        finally:
            await lifecycle.shutdown()

    task = asyncio.create_task(main())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert log == ["b", "a"]


async def test_state_is_consistent_after_a_failure_and_a_second_shutdown_is_a_no_op():
    log: list[str] = []
    lifecycle = LifecycleCoordinator()
    lifecycle.register("a", Recorder("a", log, exc=RuntimeError("boom")))
    await lifecycle.shutdown()
    await lifecycle.shutdown()
    assert log == ["a"]
    assert lifecycle._components == {} and lifecycle._started is False


# ── DockerSandbox stop contract (fake docker-py) ──────────────────────────────


class FakeContainer:
    def __init__(self, *, stop_fails=False):
        self.calls: list[str] = []
        self.stop_fails = stop_fails

    def stop(self, timeout=None):
        self.calls.append("stop")
        if self.stop_fails:
            raise RuntimeError("daemon error")

    def remove(self, force=False):
        self.calls.append("remove")


class FakeDockerClient:
    def __init__(self, container):
        self.container = container
        self.run_calls = 0
        outer = self

        class Containers:
            def run(self, **kwargs):
                outer.run_calls += 1
                if outer.container is None:
                    raise RuntimeError("cannot create")
                return outer.container

        self.containers = Containers()


def _docker_sandbox(tmp_path, monkeypatch, container):
    from velune.execution.docker_sandbox import DockerSandbox

    sandbox = DockerSandbox(tmp_path)
    client = FakeDockerClient(container)
    monkeypatch.setattr(sandbox, "_get_docker_client", lambda: client)
    monkeypatch.setattr(sandbox, "_pull_image_if_needed", lambda: None)
    return sandbox


def test_fake_docker_stop_stops_then_removes_and_is_repeatable(tmp_path, monkeypatch):
    container = FakeContainer()
    sandbox = _docker_sandbox(tmp_path, monkeypatch, container)
    sandbox.start()
    sandbox.stop()
    sandbox.stop()
    assert container.calls == ["stop", "remove"]
    assert sandbox.status == "stopped"


def test_fake_docker_a_failing_stop_still_removes_the_container(tmp_path, monkeypatch):
    container = FakeContainer(stop_fails=True)
    sandbox = _docker_sandbox(tmp_path, monkeypatch, container)
    sandbox.start()
    sandbox.stop()
    assert container.calls == ["stop", "remove"]


def test_fake_docker_a_failed_start_leaves_nothing_to_stop(tmp_path, monkeypatch):
    sandbox = _docker_sandbox(tmp_path, monkeypatch, None)
    with pytest.raises(SandboxError):
        sandbox.start()
    sandbox.stop()  # no-op
    assert sandbox.status == "stopped"


async def test_executor_shutdown_stops_a_docker_sandbox(tmp_path, monkeypatch):
    container = FakeContainer()
    sandbox = _docker_sandbox(tmp_path, monkeypatch, container)
    sandbox.start()
    executor = ExecutionExecutor(tmp_path)
    executor.sandbox = sandbox
    await executor.shutdown()
    await executor.shutdown()
    assert container.calls == ["stop", "remove"]


# ── real Docker (skipped unless a daemon is reachable and the image is local) ──


def _docker_client_or_none():
    try:
        import docker

        client = docker.from_env(timeout=3)
        client.ping()
        client.images.get("python:3.12-slim")  # never pull: no network in tests
        return client
    except Exception:
        return None


@pytest.mark.skipif(_docker_client_or_none() is None, reason="no reachable Docker daemon/image")
async def test_real_docker_shutdown_removes_only_the_sandbox_container(tmp_path):
    import docker

    from velune.execution.docker_sandbox import DockerSandbox

    client = _docker_client_or_none()
    others = {c.id for c in client.containers.list(all=True)}
    sandbox = DockerSandbox(tmp_path)
    executor = ExecutionExecutor(tmp_path)
    executor.sandbox = sandbox
    sandbox.start()
    name = sandbox.session_id
    try:
        assert client.containers.get(name).status == "running"
        await executor.shutdown()
        await executor.shutdown()
        with pytest.raises(docker.errors.NotFound):
            client.containers.get(name)
        assert others <= {c.id for c in client.containers.list(all=True)}
    finally:
        try:
            client.containers.get(name).remove(force=True)
        except Exception:
            pass
