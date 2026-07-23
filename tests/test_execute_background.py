"""Tests for ExecuteCommand's background/detached mode.

The audit's stated gap: `ExecuteCommand` was fully synchronous with no way
to start a server/watcher/long build and check on it later. Reuses the same
`JobRegistry` shape `/jobs` already lists (cognition/council jobs), so a
background shell command shows up there too.
"""

from __future__ import annotations

import asyncio
import time

import psutil
import pytest

from velune.core.task_registry import JobRegistry, JobStatus
from velune.execution.sandbox import SubprocessSandbox
from velune.tools.safety import ApprovalMode
from velune.tools.terminal.execute import ExecuteCommand


def _make_tool(tmp_path, job_registry=None):
    sandbox = SubprocessSandbox(tmp_path)
    return ExecuteCommand(
        sandbox=sandbox,
        workspace_path=str(tmp_path),
        approval_mode=ApprovalMode.SAFE,
        job_registry=job_registry,
    )


@pytest.mark.timeout(20)
async def test_background_returns_immediately_with_a_job_id(tmp_path):
    registry = JobRegistry()
    script = tmp_path / "slow.py"
    script.write_text("import time\ntime.sleep(3)\n", encoding="utf-8")
    tool = _make_tool(tmp_path, registry)

    started = time.perf_counter()
    result = await tool.execute("python slow.py", directory=str(tmp_path), background=True)
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0, "background=True must not wait for the command to finish"
    assert result["status"] == "started"
    assert "job_id" in result
    assert registry.get(result["job_id"]) is not None


@pytest.mark.timeout(20)
async def test_background_job_progresses_to_completed_with_a_result_preview(tmp_path):
    registry = JobRegistry()
    tool = _make_tool(tmp_path, registry)

    result = await tool.execute("echo background-hello", directory=str(tmp_path), background=True)
    job_id = result["job_id"]

    job = registry.get(job_id)
    assert job.task is not None
    await asyncio.wait_for(job.task, timeout=10)

    job = registry.get(job_id)
    assert job.status == JobStatus.COMPLETED
    assert "background-hello" in (job.result_preview or "")


@pytest.mark.timeout(20)
async def test_background_job_appears_in_all_jobs(tmp_path):
    registry = JobRegistry()
    tool = _make_tool(tmp_path, registry)

    result = await tool.execute("echo hi", directory=str(tmp_path), background=True)
    job = registry.get(result["job_id"])
    await asyncio.wait_for(job.task, timeout=10)

    names = [j.name for j in registry.all_jobs()]
    assert any(n.startswith("shell:") for n in names)


@pytest.mark.timeout(20)
async def test_cancelling_a_background_job_actually_kills_the_process(tmp_path):
    registry = JobRegistry()
    script = tmp_path / "sleep_long.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    tool = _make_tool(tmp_path, registry)

    pids: list[int] = []
    import velune.execution.sandbox as sandbox_mod

    original_popen = sandbox_mod.subprocess.Popen

    def _capturing_popen(*args, **kwargs):
        proc = original_popen(*args, **kwargs)
        pids.append(proc.pid)
        return proc

    sandbox_mod.subprocess.Popen = _capturing_popen
    try:
        result = await tool.execute(
            "python sleep_long.py", directory=str(tmp_path), background=True
        )
        job_id = result["job_id"]

        # Give the child process a moment to actually spawn.
        await asyncio.sleep(0.5)
        assert pids, "no process was captured"

        cancelled = registry.cancel(job_id)
        assert cancelled is True

        job = registry.get(job_id)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(job.task, timeout=10)
    finally:
        sandbox_mod.subprocess.Popen = original_popen

    time.sleep(0.3)  # let the OS reap the killed process
    assert not psutil.pid_exists(pids[0]), "the child process is still running after cancel"
    assert registry.get(job_id).status == JobStatus.CANCELLED


async def test_background_without_a_job_registry_raises_a_clear_error(tmp_path):
    tool = _make_tool(tmp_path, job_registry=None)

    with pytest.raises(RuntimeError, match="job registry"):
        await tool.execute("echo hi", directory=str(tmp_path), background=True)


async def test_foreground_execution_is_unaffected_by_the_background_param(tmp_path):
    """background=False (the default) must behave exactly as before."""
    tool = _make_tool(tmp_path, job_registry=None)

    result = await tool.execute("echo still-synchronous", directory=str(tmp_path))

    assert result["exit_code"] == 0
    assert "still-synchronous" in result["stdout"]
