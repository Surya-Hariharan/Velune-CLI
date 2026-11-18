"""Rate-limit (429) handling in the retry layer: bounded waits, no slot held while sleeping."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from velune.core.errors.provider import RateLimitError
from velune.core.retry import RetryPolicy, retry_async
from velune.providers.retrying import RetryingProvider

REQ = SimpleNamespace()


class _Flaky:
    """Fails with *errors* in order, then succeeds. Records the gate state at each call."""

    provider_id = "groq"

    def __init__(self, *errors, wrapped_ref=None):
        self._errors = list(errors)
        self.calls = 0
        self.gate_locked_during_call: list[bool] = []
        self.wrapped = wrapped_ref

    async def infer(self, request):
        self.calls += 1
        if self.wrapped is not None:
            self.gate_locked_during_call.append(self.wrapped._concurrency_gate.locked())
        if self._errors:
            raise self._errors.pop(0)
        return "ok"

    async def stream(self, request):
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        yield SimpleNamespace(content="hello")


@pytest.fixture
def sleeps(monkeypatch):
    """Record backoff sleeps without actually waiting."""
    recorded: list[float] = []

    async def fake_sleep(delay):
        recorded.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return recorded


# ── policy ────────────────────────────────────────────────────────────────────


def test_delay_for_honours_retry_after_within_the_limit():
    policy = RetryPolicy(max_retry_after_s=20.0)
    assert policy.delay_for(RateLimitError("429", retry_after=7.5), 1) == 7.5


def test_delay_for_refuses_a_wait_longer_than_the_limit():
    policy = RetryPolicy(max_retry_after_s=20.0)
    assert policy.delay_for(RateLimitError("429", retry_after=600), 1) is None


def test_delay_for_falls_back_to_backoff_without_retry_after():
    policy = RetryPolicy(jitter=False, base_delay_s=1.0)
    assert policy.delay_for(RateLimitError("429"), 2) == 2.0
    assert policy.delay_for(RuntimeError("x"), 1) == 1.0


def test_delay_for_ignores_a_garbage_retry_after():
    policy = RetryPolicy(jitter=False, base_delay_s=1.0)
    bad = RateLimitError("429")
    bad.retry_after = "soon"  # type: ignore[assignment]
    assert policy.delay_for(bad, 1) == 1.0


async def test_retry_async_fails_fast_on_an_hour_long_retry_after(sleeps):
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        raise RateLimitError("quota exhausted", retry_after=3600)

    policy = RetryPolicy(max_attempts=3, retryable_exceptions=(RateLimitError,))
    with pytest.raises(RateLimitError):
        await retry_async(policy, fn)
    assert calls == 1 and sleeps == []  # no retry, no sleep


async def test_retry_async_still_retries_a_short_retry_after(sleeps):
    errors = [RateLimitError("429", retry_after=2.0)]

    async def fn():
        if errors:
            raise errors.pop()
        return "ok"

    policy = RetryPolicy(max_attempts=3, retryable_exceptions=(RateLimitError,))
    assert await retry_async(policy, fn) == "ok"
    assert sleeps == [2.0]


# ── provider wrapper ──────────────────────────────────────────────────────────


async def test_infer_does_not_hold_the_slot_while_backing_off(monkeypatch):
    held_while_sleeping: list[bool] = []
    inner = _Flaky(RateLimitError("429", retry_after=1.0))
    wrapped = RetryingProvider(inner, max_concurrent=1)

    async def fake_sleep(delay):
        held_while_sleeping.append(wrapped._concurrency_gate.locked())

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    assert await wrapped.infer(REQ) == "ok"
    assert held_while_sleeping == [False]


async def test_a_second_caller_can_run_while_the_first_waits_out_a_429():
    inner = _Flaky(RateLimitError("429", retry_after=0.3))
    wrapped = RetryingProvider(inner, max_concurrent=1)
    first = asyncio.create_task(wrapped.infer(REQ))  # fails, then sleeps 0.3s
    await asyncio.sleep(0.05)
    assert not wrapped._concurrency_gate.locked()  # slot is free during the backoff
    assert await asyncio.wait_for(wrapped.infer(REQ), 0.2) == "ok"  # other caller not blocked
    assert await first == "ok"


async def test_infer_surfaces_a_long_retry_after_immediately(sleeps):
    inner = _Flaky(RateLimitError("quota", retry_after=600))
    wrapped = RetryingProvider(inner)
    with pytest.raises(RateLimitError):
        await wrapped.infer(REQ)
    assert inner.calls == 1 and sleeps == []


async def test_stream_retries_a_short_429_and_releases_the_slot_while_waiting(monkeypatch):
    held: list[bool] = []
    inner = _Flaky(RateLimitError("429", retry_after=1.0))
    wrapped = RetryingProvider(inner, max_concurrent=1)

    async def fake_sleep(delay):
        held.append(wrapped._concurrency_gate.locked())

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    chunks = [c.content async for c in wrapped.stream(REQ)]
    assert chunks == ["hello"] and held == [False]
    assert not wrapped._concurrency_gate.locked()  # released after the stream ends too


async def test_stream_surfaces_a_long_retry_after_immediately(sleeps):
    inner = _Flaky(RateLimitError("quota", retry_after=600))
    wrapped = RetryingProvider(inner)
    with pytest.raises(RateLimitError):
        _ = [c async for c in wrapped.stream(REQ)]
    assert inner.calls == 1 and sleeps == []


async def test_stream_gives_up_after_max_attempts(sleeps):
    inner = _Flaky(*[RateLimitError("429", retry_after=1.0)] * 5)
    wrapped = RetryingProvider(inner, max_attempts=3)
    with pytest.raises(RateLimitError):
        _ = [c async for c in wrapped.stream(REQ)]
    assert inner.calls == 3 and len(sleeps) == 2


async def test_slot_is_held_for_the_whole_successful_stream():
    seen: list[bool] = []
    inner = _Flaky()
    wrapped = RetryingProvider(inner, max_concurrent=1)
    async for _ in wrapped.stream(REQ):
        seen.append(wrapped._concurrency_gate.locked())
    assert seen == [True]
    assert not wrapped._concurrency_gate.locked()
