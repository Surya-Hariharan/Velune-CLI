"""Regression tests for fire-and-forget task tracking.

A detached ``loop.create_task(...)`` whose result is not stored anywhere can be
garbage-collected before it finishes — the loop holds only a weak reference.
``velune.core.task_registry.track`` exists to close that gap. These tests pin
the contract: tracked tasks survive until completion, are released afterward,
and surface (rather than swallow) exceptions.
"""

from __future__ import annotations

import asyncio
import gc
import logging

import pytest

from velune.core.task_registry import _TRACKED_TASKS, track


async def test_tracked_task_survives_gc_until_complete() -> None:
    """A tracked task must not be collected while pending and must run to the end."""
    ran = asyncio.Event()

    async def work() -> None:
        await asyncio.sleep(0.01)
        ran.set()

    # Do not keep our own reference — only track() should hold it alive.
    track(asyncio.create_task(work(), name="survives_gc"))
    gc.collect()  # would reap an untracked task's coroutine

    await asyncio.wait_for(ran.wait(), timeout=1.0)
    assert ran.is_set()


async def test_tracked_task_reference_released_after_completion() -> None:
    """The strong reference is dropped once the task finishes (no leak)."""

    async def work() -> None:
        return None

    task = track(asyncio.create_task(work(), name="released"))
    assert task in _TRACKED_TASKS

    await task
    # done callbacks run on the next loop cycle
    await asyncio.sleep(0)
    assert task not in _TRACKED_TASKS


async def test_tracked_task_logs_exception(caplog: pytest.LogCaptureFixture) -> None:
    """A failing tracked task logs its exception instead of dropping it silently."""

    async def boom() -> None:
        raise ValueError("kaboom")

    with caplog.at_level(logging.ERROR, logger="velune.core.task_registry"):
        task = track(asyncio.create_task(boom(), name="boomer"))
        with pytest.raises(ValueError, match="kaboom"):
            await task
        await asyncio.sleep(0)  # let the done callback fire

    assert any("boomer" in rec.message and "kaboom" in rec.message for rec in caplog.records)
    assert task not in _TRACKED_TASKS


async def test_tracked_task_cancellation_is_quiet(caplog: pytest.LogCaptureFixture) -> None:
    """Cancelling a tracked task is not reported as a failure."""

    async def forever() -> None:
        await asyncio.sleep(60)

    with caplog.at_level(logging.ERROR, logger="velune.core.task_registry"):
        task = track(asyncio.create_task(forever(), name="cancellable"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)

    assert not caplog.records
    assert task not in _TRACKED_TASKS
