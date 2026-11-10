"""Supervision of long-running background processes (dev servers, watchers).

Builds on the existing pieces rather than replacing them:

* :class:`~velune.execution.sandbox.SubprocessSandbox` still spawns and kills
  (argv-only, environment scrubbing, process-tree kill);
* :class:`~velune.core.task_registry.JobRegistry` still tracks the job.

This module adds what an agent workflow needs to *operate* a process it
started: the process id, a live bounded log, readiness checks, termination and
session cleanup. Readiness probes are deliberately local-only: a port check may
only target a loopback address, so this can never be used to scan a network.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.core.task_registry import JobRecord, JobRegistry

logger = logging.getLogger("velune.execution.processes")

#: A background job runs until it is stopped, the session ends, or this cap.
BACKGROUND_TIMEOUT_S = 12 * 60 * 60
_MAX_PATTERN_LENGTH = 200
_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})


class JobProcessObserver:
    """Feeds a sandboxed process's pid and output into its :class:`JobRecord`."""

    def __init__(self, registry: JobRegistry, job_id: str) -> None:
        self._registry = registry
        self._job_id = job_id

    def on_spawn(self, pid: int) -> None:
        started: float | None = None
        try:
            import psutil

            started = psutil.Process(pid).create_time()
        except Exception:  # identity is best effort; the status guard still applies
            started = None
        self._registry.update(self._job_id, pid=pid, pid_started=started)

    def on_output(self, stream: str, text: str) -> None:
        job = self._registry.get(self._job_id)
        if job is not None and job.log is not None:
            job.log.append(stream, text)


def process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        import psutil

        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


@dataclass(slots=True, frozen=True)
class Readiness:
    ready: bool
    reason: str
    waited_s: float


def _is_loopback(host: str) -> bool:
    host = host.strip().strip("[]").lower()
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def wait_until_ready(
    registry: JobRegistry,
    job_id: str,
    *,
    port: int | None = None,
    host: str = "127.0.0.1",
    log_pattern: str | None = None,
    timeout: float = 30.0,
    interval: float = 0.25,
) -> Readiness:
    """Wait until a background job is ready: a local port accepts connections
    and/or its output matches *log_pattern*.

    With neither condition, "ready" means "still running". A process that is not
    running is never ready — even if it printed the pattern before exiting — and
    the reason names its status and exit code. Never raises for an operational
    failure.
    """
    from velune.core.task_registry import JobStatus

    started = time.monotonic()
    timeout = max(0.0, min(float(timeout), 600.0))

    def done(ready: bool, reason: str) -> Readiness:
        if ready:
            registry.update(job_id, ready=True)
        return Readiness(ready, reason, round(time.monotonic() - started, 2))

    if registry.get(job_id) is None:
        return done(False, f"no such job: {job_id}")
    if port is not None:
        if not _is_loopback(host):
            return done(False, "readiness port checks are limited to loopback hosts")
        if not 0 < int(port) < 65536:
            return done(False, f"invalid port: {port}")
    pattern = None
    if log_pattern:
        if len(log_pattern) > _MAX_PATTERN_LENGTH:
            return done(False, "log pattern is too long")
        try:
            pattern = re.compile(log_pattern)
        except re.error as exc:
            return done(False, f"invalid log pattern: {exc}")

    while True:
        job = registry.get(job_id)
        if job is None:
            return done(False, f"no such job: {job_id}")
        if job.status not in (JobStatus.PENDING, JobStatus.RUNNING):
            code = f", exit code {job.exit_code}" if job.exit_code is not None else ""
            return done(False, f"process is no longer running ({job.status.value}{code})")
        port_ok = port is None or await asyncio.to_thread(_port_open, host, int(port))
        log_ok = pattern is None or (
            job.log is not None and pattern.search(job.log.text()) is not None
        )
        if port_ok and log_ok:
            return done(True, "ready")
        if time.monotonic() - started >= timeout:
            missing = []
            if not port_ok:
                missing.append(f"port {port} not accepting connections")
            if not log_ok:
                missing.append("log pattern not seen")
            return done(False, "timed out: " + ("; ".join(missing) or "not ready"))
        await asyncio.sleep(interval)


def terminate_job(registry: JobRegistry, job_id: str) -> bool:
    """Stop a running background job and its whole process tree.

    Returns True when the job exists. A job that has already finished is left
    exactly as it is — its status and exit code are history, and its process id
    may by now belong to an unrelated process, which must never be signalled.

    For a running job this signals the sandbox's poll loop (which kills the
    tree) and, if the process is still alive — e.g. the poll loop has not ticked
    yet — kills the tree directly so nothing outlives the request.
    """
    from velune.core.task_registry import JobStatus

    job = registry.get(job_id)
    if job is None:
        return False
    if job.status not in (JobStatus.PENDING, JobStatus.RUNNING):
        return True
    registry.cancel(job_id)
    _kill_tree(job)
    return True


def _is_same_process(job: JobRecord) -> bool:
    """True when ``job.pid`` still names the process Velune spawned for the job."""
    if not job.pid or not process_alive(job.pid):
        return False
    if job.pid_started is None:
        return True  # creation time unavailable: rely on the running-status guard
    try:
        import psutil

        return abs(psutil.Process(job.pid).create_time() - job.pid_started) < 1.0
    except Exception:
        return False


def _kill_tree(job: JobRecord) -> None:
    if _is_same_process(job):
        from velune.execution.sandbox import _kill_process_tree

        try:
            _kill_process_tree(job.pid)
        except Exception as exc:  # best effort; the poll loop kills it too
            logger.debug("kill of job %s (pid %s) failed: %s", job.job_id, job.pid, exc)
    elif job.pid:
        logger.debug(
            "job %s: pid %s is no longer the job's process; not signalling", job.job_id, job.pid
        )


def cleanup_background_jobs(registry: JobRegistry | None, kind: str = "shell") -> int:
    """Terminate every still-running supervised job (called on session exit).

    Returns how many were stopped. A background process must never outlive
    the session that started it unnoticed.
    """
    if registry is None:
        return 0
    stopped = 0
    for job in registry.running(kind):
        if terminate_job(registry, job.job_id):
            stopped += 1
    return stopped
