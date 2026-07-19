"""CouncilScheduler — the single owner of concurrency decisions for council rounds.

No prior test coverage existed for this despite it underpinning a real fix: the
council's critique fan-out (reviewer + up to 5 critics) used to call a bare
``asyncio.gather`` directly in ``CouncilOrchestrator``, firing every agent at
once regardless of whether they shared a backend. On a single local
Ollama/LM Studio instance — the common case on low-end hardware — that isn't
real parallelism, and forcing N simultaneous generation requests against one
weak machine risks OOM/thrashing rather than any wall-clock benefit. The fix
routes that fan-out through this scheduler, which serializes same-backend jobs
and only runs distinct backends concurrently. These tests pin down that the
scheduler actually delivers on that contract.
"""

from __future__ import annotations

import asyncio

import pytest

from velune.cognition.council.scheduler import CouncilJob, CouncilScheduler


@pytest.mark.asyncio
async def test_same_backend_jobs_never_overlap_in_time():
    """The physical truth this scheduler exists to be honest about."""
    scheduler = CouncilScheduler()
    active = 0
    max_concurrent = 0

    async def _work(tag: str):
        nonlocal active, max_concurrent
        active += 1
        max_concurrent = max(max_concurrent, active)
        await asyncio.sleep(0.01)
        active -= 1
        return tag

    jobs = [
        CouncilJob(name=f"job{i}", provider_id="ollama", run=lambda i=i: _work(f"job{i}"))
        for i in range(4)
    ]
    results = await scheduler.run(jobs)

    assert max_concurrent == 1, "same-backend jobs must serialize, never overlap"
    assert scheduler.last_mode == "sequential"
    assert [r.value for r in results] == ["job0", "job1", "job2", "job3"]


@pytest.mark.asyncio
async def test_distinct_backend_jobs_run_concurrently():
    scheduler = CouncilScheduler()
    active = 0
    max_concurrent = 0

    async def _work(tag: str):
        nonlocal active, max_concurrent
        active += 1
        max_concurrent = max(max_concurrent, active)
        await asyncio.sleep(0.02)
        active -= 1
        return tag

    jobs = [
        CouncilJob(name="a", provider_id="anthropic", run=lambda: _work("a")),
        CouncilJob(name="b", provider_id="groq", run=lambda: _work("b")),
        CouncilJob(name="c", provider_id="openai", run=lambda: _work("c")),
    ]
    results = await scheduler.run(jobs)

    assert max_concurrent == 3
    assert scheduler.last_mode == "concurrent"
    assert [r.name for r in results] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_mixed_backends_serialize_within_group_concurrent_across_groups():
    scheduler = CouncilScheduler()

    jobs = [
        CouncilJob(name="reviewer", provider_id="ollama", run=lambda: _identity("reviewer")),
        CouncilJob(name="challenger", provider_id="ollama", run=lambda: _identity("challenger")),
        CouncilJob(name="security", provider_id="groq", run=lambda: _identity("security")),
    ]
    results = await scheduler.run(jobs)

    assert scheduler.last_mode == "mixed"
    # Order is preserved regardless of grouping/execution mode.
    assert [r.name for r in results] == ["reviewer", "challenger", "security"]


async def _identity(x):
    return x


@pytest.mark.asyncio
async def test_one_job_failing_does_not_abort_the_round():
    scheduler = CouncilScheduler()

    async def _boom():
        raise ValueError("agent exploded")

    async def _fine():
        return "ok"

    jobs = [
        CouncilJob(name="broken", provider_id="ollama", run=_boom),
        CouncilJob(name="fine", provider_id="ollama", run=_fine),
    ]
    results = await scheduler.run(jobs)

    by_name = {r.name: r for r in results}
    assert by_name["broken"].ok is False
    assert "agent exploded" in by_name["broken"].error
    assert by_name["fine"].ok is True
    assert by_name["fine"].value == "ok"


@pytest.mark.asyncio
async def test_job_exceeding_timeout_reports_timeout_status():
    scheduler = CouncilScheduler()

    async def _slow():
        await asyncio.sleep(0.5)
        return "too late"

    jobs = [CouncilJob(name="slow", provider_id="ollama", run=_slow)]
    results = await scheduler.run(jobs, timeout=0.01)

    assert results[0].status == "timeout"
    assert results[0].ok is False


@pytest.mark.asyncio
async def test_empty_job_list_is_idle():
    scheduler = CouncilScheduler()
    assert await scheduler.run([]) == []
    assert scheduler.last_mode == "idle"


@pytest.mark.asyncio
async def test_force_sequential_overrides_distinct_backends():
    scheduler = CouncilScheduler(force_sequential=True)
    active = 0
    max_concurrent = 0

    async def _work():
        nonlocal active, max_concurrent
        active += 1
        max_concurrent = max(max_concurrent, active)
        await asyncio.sleep(0.01)
        active -= 1

    jobs = [
        CouncilJob(name="a", provider_id="anthropic", run=_work),
        CouncilJob(name="b", provider_id="groq", run=_work),
    ]
    await scheduler.run(jobs)

    assert max_concurrent == 1
    assert scheduler.last_mode == "sequential"
