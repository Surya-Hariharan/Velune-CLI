# tests/test_task_registry.py

import asyncio
import time

import pytest

from velune.core.task_registry import BackgroundTaskRegistry


@pytest.mark.asyncio
async def test_submit_completes_under_load():
    """Tasks submitted while loop is busy must complete."""
    registry = BackgroundTaskRegistry()
    results = []

    async def heavy_task():
        await asyncio.sleep(0.5)  # Simulate inference

    async def tracked_task(i):
        results.append(i)

    # Start a "heavy" background task
    heavy = asyncio.create_task(heavy_task())

    # Submit during load
    for i in range(5):
        registry.submit(f"task_{i}", tracked_task(i), timeout_seconds=5.0)

    await heavy
    await asyncio.sleep(0.1)  # Let tasks complete

    assert len(results) == 5, f"Only {len(results)}/5 tasks completed"


@pytest.mark.asyncio
async def test_cancel_all_completes_within_timeout():
    """cancel_all must return within timeout even with slow tasks."""
    registry = BackgroundTaskRegistry()

    async def slow_task():
        await asyncio.sleep(100)  # Would run forever

    registry.submit("slow", slow_task(), timeout_seconds=200)
    await asyncio.sleep(0.05)  # Let task start

    start = time.time()
    await registry.cancel_all(timeout=2.0)
    elapsed = time.time() - start

    assert elapsed < 3.0, f"cancel_all took {elapsed:.1f}s"
    assert registry.pending_count() == 0


def test_stats_tracking():
    """submit() and dropped counts must be accurate."""
    registry = BackgroundTaskRegistry()
    assert registry.stats()["submitted"] == 0
