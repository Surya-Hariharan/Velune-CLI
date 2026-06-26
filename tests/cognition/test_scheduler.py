"""Tests for CouncilScheduler: failure isolation, ordering, honest mode."""

from __future__ import annotations

import asyncio

import pytest

from velune.cognition.council.scheduler import CouncilJob, CouncilScheduler


def _job(name: str, provider: str, value=None, *, fail=False, hang=False) -> CouncilJob:
    async def run():
        if hang:
            await asyncio.sleep(10)
        if fail:
            raise RuntimeError(f"{name} boom")
        return value if value is not None else name

    return CouncilJob(name=name, provider_id=provider, run=run)


def test_shared_backend_runs_sequentially() -> None:
    sched = CouncilScheduler()
    jobs = [_job("a", "ollama"), _job("b", "ollama"), _job("c", "ollama")]
    results = asyncio.run(sched.run(jobs))
    assert sched.last_mode == "sequential"
    assert [r.name for r in results] == ["a", "b", "c"]
    assert all(r.ok for r in results)


def test_distinct_backends_run_concurrently() -> None:
    sched = CouncilScheduler()
    jobs = [_job("a", "ollama"), _job("b", "openai"), _job("c", "anthropic")]
    results = asyncio.run(sched.run(jobs))
    assert sched.last_mode == "concurrent"
    assert {r.name for r in results} == {"a", "b", "c"}


def test_results_preserve_input_order_across_backends() -> None:
    sched = CouncilScheduler()
    jobs = [_job("a", "ollama"), _job("b", "openai"), _job("c", "ollama")]
    results = asyncio.run(sched.run(jobs))
    assert [r.name for r in results] == ["a", "b", "c"]
    assert sched.last_mode == "mixed"  # ollama carried two jobs


def test_one_failure_does_not_abort_the_round() -> None:
    sched = CouncilScheduler()
    jobs = [_job("a", "ollama"), _job("b", "ollama", fail=True), _job("c", "ollama")]
    results = asyncio.run(sched.run(jobs))
    by_name = {r.name: r for r in results}
    assert by_name["a"].ok
    assert by_name["c"].ok
    assert by_name["b"].status == "error"
    assert "boom" in by_name["b"].error


def test_timeout_is_isolated() -> None:
    sched = CouncilScheduler()
    jobs = [_job("fast", "openai", value="ok"), _job("slow", "anthropic", hang=True)]
    results = asyncio.run(sched.run(jobs, timeout=0.05))
    by_name = {r.name: r for r in results}
    assert by_name["fast"].ok
    assert by_name["slow"].status == "timeout"


def test_force_sequential_overrides_distinct_backends() -> None:
    sched = CouncilScheduler(force_sequential=True)
    jobs = [_job("a", "openai"), _job("b", "anthropic")]
    results = asyncio.run(sched.run(jobs))
    assert sched.last_mode == "sequential"
    assert [r.name for r in results] == ["a", "b"]


def test_empty_jobs() -> None:
    sched = CouncilScheduler()
    assert asyncio.run(sched.run([])) == []
    assert sched.last_mode == "idle"


@pytest.mark.parametrize("provider", ["ollama", "lmstudio"])
def test_single_backend_modes(provider: str) -> None:
    sched = CouncilScheduler()
    asyncio.run(sched.run([_job("only", provider)]))
    assert sched.last_mode == "sequential"
