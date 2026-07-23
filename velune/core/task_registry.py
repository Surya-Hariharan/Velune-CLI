"""Structured concurrency manager for tracking, timing out, and cancelling background tasks."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from velune._compat import StrEnum

T = TypeVar("T")
logger = logging.getLogger("velune.core.task_registry")

# Module-level strong references to fire-and-forget tasks.
#
# ``loop.create_task`` only stores a *weak* reference to the task it returns.
# If the caller does not keep a strong reference, the task can be garbage
# collected mid-execution — the work is silently dropped and asyncio emits a
# "Task was destroyed but it is pending!" warning. Sites that genuinely cannot
# await a coroutine (signal handlers, daemon accept loops, post-turn telemetry)
# must route through :func:`track` so the reference survives until completion.
_TRACKED_TASKS: set[asyncio.Task[Any]] = set()


def track(task: asyncio.Task[Any]) -> asyncio.Task[Any]:
    """Hold a strong reference to a fire-and-forget *task* until it finishes.

    Prevents premature garbage collection of detached tasks and surfaces any
    exception they raise (which would otherwise be swallowed because nobody
    awaits the task). Returns the same task so callers can still inspect it.
    """
    _TRACKED_TASKS.add(task)

    def _on_done(completed: asyncio.Task[Any]) -> None:
        _TRACKED_TASKS.discard(completed)
        if completed.cancelled():
            return
        exc = completed.exception()
        if exc is not None:
            logger.error(
                "Fire-and-forget task '%s' failed: %s",
                completed.get_name(),
                exc,
                exc_info=exc,
            )

    task.add_done_callback(_on_done)
    return task


async def cancel_tracked(timeout: float = 5.0) -> None:
    """Cancel every task registered via :func:`track` and wait for them to stop.

    Shutdown closes shared resources — the SQLite pool, LanceDB, the embedding
    pipeline — while fire-and-forget tasks may still be mid-write. Those tasks
    live in ``_TRACKED_TASKS``, which ``BackgroundTaskRegistry.cancel_all`` does
    not see, so without this they ran on against closed handles until the event
    loop tore them down. Call this *before* lifecycle shutdown.
    """
    pending = [t for t in list(_TRACKED_TASKS) if not t.done()]
    if not pending:
        return

    for task in pending:
        task.cancel()

    done, still_pending = await asyncio.wait(pending, timeout=timeout)
    if still_pending:
        logger.warning(
            "%d tracked task(s) did not stop within %.1fs: %s",
            len(still_pending),
            timeout,
            ", ".join(sorted(t.get_name() for t in still_pending)),
        )


def tracked_task_count() -> int:
    """Number of live fire-and-forget tasks. Exposed for diagnostics and tests."""
    return len([t for t in _TRACKED_TASKS if not t.done()])


class BackgroundTaskRegistry:
    """Tracks in-flight asyncio tasks with timeout and cancellation support."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = threading.Lock()
        self._submitted_count = 0
        self._dropped_count = 0

    def submit(
        self,
        name: str,
        coro: Awaitable[T],
        timeout_seconds: float = 60.0,
        on_error: Callable[[Exception], None] | None = None,
    ) -> asyncio.Task | None:
        """Submit a named background task from an async context only."""
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.error(
                "BackgroundTaskRegistry.submit('%s') called outside an async context. "
                "Background tasks must be submitted from within a running event loop.",
                name,
            )
            with self._lock:
                self._dropped_count += 1
            return None

        with self._lock:
            self._submitted_count += 1

        async def _wrapped() -> Any:
            try:
                result = await asyncio.wait_for(coro, timeout=timeout_seconds)
                logger.debug("Background task '%s' completed.", name)
                return result
            except TimeoutError:
                logger.warning("Background task '%s' timed out after %.1fs", name, timeout_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Background task '%s' failed: %s", name, exc)
                if on_error:
                    try:
                        on_error(exc)
                    except Exception as inner:
                        logger.error("Error handler for task '%s' failed: %s", name, inner)
            finally:
                with self._lock:
                    self._tasks.pop(name, None)

        task = running_loop.create_task(_wrapped(), name=name)
        with self._lock:
            self._tasks[name] = task
        return task

    async def cancel_all(self, timeout: float = 5.0) -> None:
        """Cancel all pending tasks and wait for completion."""
        with self._lock:
            tasks = [t for t in self._tasks.values() if not t.done()]

        if not tasks:
            return

        for task in tasks:
            task.cancel()

        # Give tasks a chance to handle CancelledError
        done, pending = await asyncio.wait(tasks, timeout=timeout)

        if pending:
            logger.warning("%d background tasks did not cancel within %.1fs", len(pending), timeout)

        # Shutdown stats reporting of dropped tasks
        stats = self.stats()
        if stats["dropped"] > 0:
            logger.info("Task registry shutdown: %d dropped tasks reported.", stats["dropped"])

        with self._lock:
            self._tasks.clear()

    def pending_count(self) -> int:
        """Expose pending tasks count for health checks."""
        with self._lock:
            return len([t for t in self._tasks.values() if not t.done()])

    def is_healthy(self) -> bool:
        """Check if task registry is healthy."""
        try:
            loop = asyncio.get_running_loop()
            loop_alive = loop.is_running()
        except Exception:
            loop_alive = False
        return loop_alive and self.pending_count() < 50

    def stats(self) -> dict[str, int]:
        """Return task submission telemetry stats."""
        with self._lock:
            return {
                "submitted": self._submitted_count,
                "dropped": self._dropped_count,
            }


# ---------------------------------------------------------------------------
# User-visible background job registry (for /run --bg tasks)
# ---------------------------------------------------------------------------


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class JobRecord:
    """Tracks the lifecycle of a single user-submitted background job."""

    job_id: str
    name: str
    status: JobStatus = JobStatus.PENDING
    submitted_at: float = field(default_factory=time.monotonic)
    completed_at: float | None = None
    current_phase: str | None = None
    result_preview: str | None = None
    error: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)
    #: Set by `JobRegistry.cancel()` alongside `task.cancel()`. `task.cancel()`
    #: alone cannot interrupt a shell command already running inside
    #: `asyncio.to_thread` (see `velune.execution.cancellation`) — a
    #: background job wrapping one needs this to actually kill the OS
    #: process instead of leaving it running past the job's "cancelled"
    #: status.
    cancel_event: threading.Event | None = field(default=None, repr=False)


class JobRegistry:
    """Thread-safe store of user-facing background job records."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.Lock()
        self._next_id: int = 1

    def new_id(self) -> str:
        with self._lock:
            jid = f"job-{self._next_id:04d}"
            self._next_id += 1
            return jid

    def register(self, job: JobRecord) -> None:
        with self._lock:
            self._jobs[job.job_id] = job

    def update(self, job_id: str, **kwargs: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                for k, v in kwargs.items():
                    setattr(job, k, v)

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self._jobs.get(job_id)

    def all_jobs(self) -> list[JobRecord]:
        with self._lock:
            return list(self._jobs.values())

    def active_count(self) -> int:
        with self._lock:
            return sum(
                1 for j in self._jobs.values() if j.status in (JobStatus.PENDING, JobStatus.RUNNING)
            )

    def cancel(self, job_id: str) -> bool:
        """Cancel the asyncio task for *job_id*; return True if cancelled."""
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.task is not None and not job.task.done():
            job.task.cancel()
        if job.cancel_event is not None:
            job.cancel_event.set()
        self.update(job_id, status=JobStatus.CANCELLED, completed_at=time.monotonic())
        return True
