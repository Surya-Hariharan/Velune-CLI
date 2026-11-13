"""Background process supervision: identity, logs, readiness, termination, cleanup."""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path

import pytest

from velune.core.task_registry import JobRecord, JobRegistry, JobStatus, ProcessLog
from velune.execution.processes import (
    cleanup_background_jobs,
    process_alive,
    terminate_job,
    wait_until_ready,
)

# ── ProcessLog ──────────────────────────────────────────────────────────────


def test_process_log_reassembles_lines_across_chunks():
    log = ProcessLog()
    log.append("stdout", "Listen")
    log.append("stdout", "ing on 8000\r\nsecond\n")
    log.append("stderr", "warn\n")
    assert log.tail(10) == ["Listening on 8000", "second", "[stderr] warn"]
    assert log.tail(10, "stderr") == ["warn"]


def test_process_log_is_bounded():
    log = ProcessLog(max_lines=3, max_chars=1000)
    for i in range(10):
        log.append("stdout", f"line {i}\n")
    assert log.tail(10) == ["line 7", "line 8", "line 9"]
    assert log.dropped == 7 and log.total_lines == 10


def test_process_log_bounds_characters_and_flushes_partials():
    log = ProcessLog(max_lines=100, max_chars=10)
    log.append("stdout", "aaaaaa\nbbbbbb\n")
    assert log.tail(5) == ["bbbbbb"]
    log.append("stdout", "no newline")
    log.flush()
    assert log.tail(1) == ["no newline"]


def test_registry_logs_and_running():
    reg = JobRegistry()
    reg.register(JobRecord("job-1", "shell:x", kind="shell", log=ProcessLog()))
    reg.register(JobRecord("job-2", "task", kind="task"))
    reg.get("job-1").log.append("stdout", "hi\n")
    assert reg.logs("job-1") == ["hi"]
    assert reg.logs("job-2") is None and reg.logs("missing") is None
    assert [j.job_id for j in reg.running("shell")] == ["job-1"]


# ── Readiness ───────────────────────────────────────────────────────────────


def _running_job(reg: JobRegistry, job_id="job-1") -> JobRecord:
    job = JobRecord(job_id, "shell:srv", kind="shell", log=ProcessLog(), status=JobStatus.RUNNING)
    reg.register(job)
    return job


async def test_ready_when_the_log_pattern_appears():
    reg = JobRegistry()
    job = _running_job(reg)

    async def later():
        await asyncio.sleep(0.1)
        job.log.append("stdout", "Uvicorn running on http://127.0.0.1:8000\n")

    asyncio.create_task(later())
    result = await wait_until_ready(
        reg, "job-1", log_pattern=r"running on", timeout=5, interval=0.02
    )
    assert result.ready and reg.get("job-1").ready is True


async def test_ready_when_a_local_port_accepts_connections():
    reg = JobRegistry()
    _running_job(reg)
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    try:
        port = server.getsockname()[1]
        result = await wait_until_ready(reg, "job-1", port=port, timeout=5, interval=0.02)
        assert result.ready
    finally:
        server.close()


async def test_port_checks_are_limited_to_loopback():
    reg = JobRegistry()
    _running_job(reg)
    for host in ("10.0.0.5", "example.com", "169.254.169.254"):
        result = await wait_until_ready(reg, "job-1", host=host, port=80, timeout=1)
        assert not result.ready and "loopback" in result.reason


async def test_readiness_fails_fast_when_the_process_exits():
    reg = JobRegistry()
    job = _running_job(reg)
    job.status = JobStatus.FAILED
    result = await wait_until_ready(reg, "job-1", log_pattern="never", timeout=10)
    assert not result.ready and "no longer running" in result.reason and result.waited_s < 2


async def test_readiness_times_out_with_a_reason():
    reg = JobRegistry()
    _running_job(reg)
    result = await wait_until_ready(reg, "job-1", log_pattern="never", timeout=0.2, interval=0.05)
    assert not result.ready and "log pattern not seen" in result.reason


@pytest.mark.parametrize("pattern", ["(", "x" * 500])
async def test_bad_patterns_are_rejected(pattern):
    reg = JobRegistry()
    _running_job(reg)
    assert not (await wait_until_ready(reg, "job-1", log_pattern=pattern, timeout=1)).ready


async def test_unknown_job():
    assert not (await wait_until_ready(JobRegistry(), "nope", timeout=1)).ready
    assert terminate_job(JobRegistry(), "nope") is False


# ── End to end with a real process ──────────────────────────────────────────


@pytest.fixture
def trusted_python(monkeypatch):
    """Make `python` resolve to this interpreter's own (trusted) environment."""
    scripts = Path(sys.executable).parent
    monkeypatch.setenv("PATH", str(scripts) + os.pathsep + os.environ.get("PATH", ""))


async def _settle(task) -> None:
    """Wait for a (possibly cancelled) supervising task to finish."""
    done, _ = await asyncio.wait({task}, timeout=10)
    assert done, "supervising task did not finish"


def _tool(tmp_path, registry):
    from velune.execution.sandbox import SubprocessSandbox
    from velune.tools.safety import ApprovalMode
    from velune.tools.terminal.execute import ExecuteCommand

    return ExecuteCommand(
        sandbox=SubprocessSandbox(tmp_path),
        workspace_path=str(tmp_path),
        approval_mode=ApprovalMode.SAFE,
        job_registry=registry,
    )


async def _start_server(tmp_path, registry):
    (tmp_path / "srv.py").write_text(
        "import time, sys\nprint('server ready', flush=True)\n"
        "for i in range(600):\n    print(f'tick {i}', flush=True)\n    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    started = await _tool(tmp_path, registry).execute("python srv.py", background=True)
    return started["job_id"]


async def test_background_process_is_identified_logged_ready_and_terminated(
    tmp_path, trusted_python
):
    registry = JobRegistry()
    job_id = await _start_server(tmp_path, registry)
    ready = await wait_until_ready(registry, job_id, log_pattern="server ready", timeout=20)
    assert ready.ready, ready.reason

    job = registry.get(job_id)
    assert job.pid and process_alive(job.pid)
    assert job.command == "python srv.py" and job.owner == "agent"
    await asyncio.sleep(0.2)
    assert any(line.startswith("tick") for line in registry.logs(job_id, 50))

    pid = job.pid
    assert terminate_job(registry, job_id)
    await _settle(job.task)
    assert not process_alive(pid)
    assert registry.get(job_id).status is JobStatus.CANCELLED


async def test_background_job_outlives_the_foreground_timeout(tmp_path, trusted_python):
    """A server must not be killed by the 30s foreground default (or any short cap)."""
    registry = JobRegistry()
    job_id = await _start_server(tmp_path, registry)
    assert (await wait_until_ready(registry, job_id, log_pattern="server ready", timeout=20)).ready
    from velune.execution.processes import BACKGROUND_TIMEOUT_S

    assert BACKGROUND_TIMEOUT_S > 30
    terminate_job(registry, job_id)
    await _settle(registry.get(job_id).task)


async def test_foreground_ctrl_c_does_not_kill_background_jobs(tmp_path, trusted_python):
    from velune.execution import cancellation

    registry = JobRegistry()
    job_id = await _start_server(tmp_path, registry)
    assert (await wait_until_ready(registry, job_id, log_pattern="server ready", timeout=20)).ready
    cancellation.cancel_all()  # what Ctrl+C on a foreground command does
    await asyncio.sleep(0.3)
    job = registry.get(job_id)
    assert job.status is JobStatus.RUNNING and process_alive(job.pid)
    assert cleanup_background_jobs(registry) == 1
    await _settle(job.task)
    assert not process_alive(job.pid)


async def test_cleanup_without_a_registry_is_a_noop():
    assert cleanup_background_jobs(None) == 0


# ── Process identity: a pid alone is not proof of ownership ──────────────────


def _child():
    import subprocess

    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _create_time(pid: int) -> float:
    import psutil

    return psutil.Process(pid).create_time()


def test_terminate_leaves_a_finished_job_untouched():
    """A finished job's pid may belong to anyone now — here it is this very process."""
    reg = JobRegistry()
    job = JobRecord("job-1", "shell:x", kind="shell", status=JobStatus.COMPLETED, pid=os.getpid())
    job.exit_code = 0
    reg.register(job)
    assert terminate_job(reg, "job-1") is True
    after = reg.get("job-1")
    assert after.status is JobStatus.COMPLETED and after.exit_code == 0  # history intact
    assert process_alive(os.getpid())


def test_terminate_does_not_signal_a_recycled_pid():
    child = _child()
    try:
        reg = JobRegistry()
        # The job believes pid X started long ago; the live process with that pid is newer.
        job = JobRecord(
            "job-1",
            "shell:x",
            kind="shell",
            status=JobStatus.RUNNING,
            pid=child.pid,
            pid_started=_create_time(child.pid) - 3600,
        )
        reg.register(job)
        assert terminate_job(reg, "job-1")
        assert child.poll() is None and process_alive(child.pid), "an unrelated process was killed"
    finally:
        child.kill()
        child.wait(timeout=10)


def test_terminate_kills_the_process_it_started():
    child = _child()
    try:
        reg = JobRegistry()
        reg.register(
            JobRecord(
                "job-1",
                "shell:x",
                kind="shell",
                status=JobStatus.RUNNING,
                pid=child.pid,
                pid_started=_create_time(child.pid),
            )
        )
        assert terminate_job(reg, "job-1")
        assert child.wait(timeout=10) is not None
        assert not process_alive(child.pid)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


async def test_a_process_that_exited_is_not_ready_even_if_it_printed_the_pattern():
    reg = JobRegistry()
    job = _running_job(reg)
    job.log.append("stdout", "server ready\n")
    job.status = JobStatus.FAILED
    job.exit_code = 3
    result = await wait_until_ready(reg, "job-1", log_pattern="server ready", timeout=5)
    assert not result.ready
    assert "no longer running" in result.reason and "exit code 3" in result.reason


async def test_spawn_records_the_process_creation_time(tmp_path, trusted_python):
    registry = JobRegistry()
    job_id = await _start_server(tmp_path, registry)
    assert (await wait_until_ready(registry, job_id, log_pattern="server ready", timeout=20)).ready
    job = registry.get(job_id)
    assert job.pid_started is not None
    assert abs(_create_time(job.pid) - job.pid_started) < 1.0
    terminate_job(registry, job_id)
    await _settle(job.task)
