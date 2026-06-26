"""Council execution scheduler — the single owner of concurrency decisions.

The previous code scattered ``asyncio.gather`` across the orchestrator and
implicitly claimed parallelism. On a single local GPU that claim is false: every
request to the one Ollama/LM Studio backend serializes, so gathering N critics
buys no wall-clock win and only obscures what actually happened.

``CouncilScheduler`` centralizes the decision and is *honest* about it:

- Jobs are grouped by ``provider_id``.
- Jobs sharing a backend run **sequentially** (that is the physical truth).
- Jobs on **distinct** backends run **concurrently** via ``asyncio.gather``.
- One job failing (exception or timeout) never aborts the round — each job
  returns an isolated :class:`JobResult` carrying ``ok`` / ``error`` / ``timeout``.

``last_mode`` records whether the round actually ran ``sequential``,
``concurrent``, or ``mixed`` so the UI can tell the user the truth.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from velune.core.trace import TracedLogger

logger = TracedLogger("velune.cognition.council.scheduler")


@dataclass
class CouncilJob:
    """A unit of council work bound to the backend that will run it.

    Args:
        name: Stable identifier (e.g. ``"reviewer"``, ``"coder#1"``).
        provider_id: The backend the job runs on. Jobs sharing this value
            serialize; distinct values may run concurrently.
        run: Zero-arg factory returning the awaitable to execute. A factory (not
            a bare coroutine) lets the scheduler control *when* work starts and
            avoids "coroutine was never awaited" warnings.
    """

    name: str
    provider_id: str
    run: Callable[[], Awaitable[Any]]


@dataclass
class JobResult:
    """Isolated outcome of a single :class:`CouncilJob`."""

    name: str
    status: str  # "ok" | "error" | "timeout"
    value: Any | None = None
    error: str | None = None
    elapsed_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class CouncilScheduler:
    """Runs council jobs with honest concurrency and per-job failure isolation."""

    def __init__(self, force_sequential: bool = False) -> None:
        self.force_sequential = force_sequential
        self.last_mode: str = "idle"

    async def _run_one(self, job: CouncilJob, timeout: float | None) -> JobResult:
        start = time.perf_counter()
        try:
            awaitable = job.run()
            value = (
                await asyncio.wait_for(awaitable, timeout=timeout)
                if timeout is not None
                else await awaitable
            )
            return JobResult(
                name=job.name,
                status="ok",
                value=value,
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("Job %s timed out after %.0fs", job.name, timeout or 0)
            return JobResult(
                name=job.name,
                status="timeout",
                error=f"timed out after {timeout}s",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        except Exception as exc:  # isolate: one failure must not abort the round
            logger.error("Job %s failed: %s", job.name, exc)
            return JobResult(
                name=job.name,
                status="error",
                error=str(exc),
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )

    async def _run_group_sequential(
        self, jobs: list[CouncilJob], timeout: float | None
    ) -> list[JobResult]:
        results: list[JobResult] = []
        for job in jobs:
            results.append(await self._run_one(job, timeout))
        return results

    async def run(self, jobs: list[CouncilJob], timeout: float | None = None) -> list[JobResult]:
        """Execute *jobs* and return results in the original input order.

        Results preserve the order of ``jobs`` regardless of execution mode so
        callers can zip them back to their inputs deterministically.
        """
        if not jobs:
            self.last_mode = "idle"
            return []

        # Group by backend, preserving first-seen order.
        groups: dict[str, list[CouncilJob]] = {}
        for job in jobs:
            groups.setdefault(job.provider_id, []).append(job)

        index = {id(job): i for i, job in enumerate(jobs)}
        ordered: list[JobResult | None] = [None] * len(jobs)

        if self.force_sequential or len(groups) == 1:
            self.last_mode = "sequential"
            flat = await self._run_group_sequential(jobs, timeout)
            for job, res in zip(jobs, flat, strict=True):
                ordered[index[id(job)]] = res
        else:
            self.last_mode = "concurrent" if len(groups) > 1 else "sequential"
            # Each distinct backend runs its own jobs sequentially; backends run
            # concurrently relative to each other.
            group_jobs = list(groups.values())
            group_results = await asyncio.gather(
                *(self._run_group_sequential(g, timeout) for g in group_jobs)
            )
            for g, res_list in zip(group_jobs, group_results, strict=True):
                for job, res in zip(g, res_list, strict=True):
                    ordered[index[id(job)]] = res
            # "mixed" when at least one backend carried more than one job.
            if any(len(g) > 1 for g in group_jobs):
                self.last_mode = "mixed"

        return [r for r in ordered if r is not None]
