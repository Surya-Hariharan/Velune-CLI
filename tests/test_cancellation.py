"""Tests for the inference cancellation primitives."""

import asyncio
import pytest
from unittest.mock import MagicMock

from velune.execution.cancellation import CancellationToken, InferenceGuard


@pytest.mark.asyncio
async def test_token_starts_not_cancelled():
    t = CancellationToken()
    assert not t.is_cancelled


@pytest.mark.asyncio
async def test_token_cancel_sets_flag():
    t = CancellationToken()
    t.cancel()
    assert t.is_cancelled


@pytest.mark.asyncio
async def test_token_wait_returns_after_cancel():
    t = CancellationToken()

    async def _cancel_soon():
        await asyncio.sleep(0)
        t.cancel()

    asyncio.create_task(_cancel_soon())
    await t.wait()
    assert t.is_cancelled


@pytest.mark.asyncio
async def test_guard_catches_keyboard_interrupt():
    console = MagicMock()
    guard = InferenceGuard(console)
    async with guard.guard():
        raise KeyboardInterrupt()
    console.print.assert_called()


@pytest.mark.asyncio
async def test_guard_catches_cancelled_error():
    console = MagicMock()
    guard = InferenceGuard(console)
    async with guard.guard():
        raise asyncio.CancelledError()
    console.print.assert_called()


@pytest.mark.asyncio
async def test_guard_clears_token_after_completion():
    console = MagicMock()
    guard = InferenceGuard(console)
    async with guard.guard() as token:
        assert token is not None
    assert guard._current_token is None


@pytest.mark.asyncio
async def test_guard_clears_token_after_keyboard_interrupt():
    console = MagicMock()
    guard = InferenceGuard(console)
    async with guard.guard():
        raise KeyboardInterrupt()
    assert guard._current_token is None


@pytest.mark.asyncio
async def test_abort_cancels_running_token():
    console = MagicMock()
    guard = InferenceGuard(console)
    async with guard.guard() as token:
        guard.abort()
        assert token.is_cancelled


@pytest.mark.asyncio
async def test_abort_is_noop_when_no_token():
    console = MagicMock()
    guard = InferenceGuard(console)
    # Should not raise even with no active guard
    guard.abort()


@pytest.mark.asyncio
async def test_guard_token_is_none_before_entering():
    console = MagicMock()
    guard = InferenceGuard(console)
    assert guard._current_token is None


@pytest.mark.asyncio
async def test_guard_normal_completion_no_print():
    console = MagicMock()
    guard = InferenceGuard(console)
    async with guard.guard():
        pass  # no exception
    console.print.assert_not_called()
