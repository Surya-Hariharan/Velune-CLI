"""Tests for RetryPolicy and retry_async."""

from __future__ import annotations

import asyncio

import pytest

from velune.core.loop_detector import ErrorLoopDetector
from velune.core.retry import RetryPolicy, retry_async


@pytest.mark.asyncio
class TestRetryAsync:
    async def test_succeeds_on_first_attempt(self):
        calls = []

        async def fn():
            calls.append(1)
            return "ok"

        policy = RetryPolicy(max_attempts=3, base_delay_s=0)
        result = await retry_async(policy, fn)
        assert result == "ok"
        assert len(calls) == 1

    async def test_retries_then_succeeds(self):
        calls = []

        async def fn():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("transient")
            return "ok"

        policy = RetryPolicy(max_attempts=3, base_delay_s=0, jitter=False)
        result = await retry_async(policy, fn)
        assert result == "ok"
        assert len(calls) == 3

    async def test_raises_after_max_attempts_exhausted(self):
        async def fn():
            raise RuntimeError("always fails")

        policy = RetryPolicy(max_attempts=3, base_delay_s=0, jitter=False)
        with pytest.raises(RuntimeError, match="always fails"):
            await retry_async(policy, fn)

    async def test_cancelled_error_never_retried(self):
        calls = []

        async def fn():
            calls.append(1)
            raise asyncio.CancelledError()

        policy = RetryPolicy(max_attempts=3, base_delay_s=0)
        with pytest.raises(asyncio.CancelledError):
            await retry_async(policy, fn)
        assert len(calls) == 1

    async def test_loop_detection_aborts_early(self):
        detector = ErrorLoopDetector()
        # Pre-fill loop state so the NEXT record call triggers is_looping
        exc = RuntimeError("looping")
        for _ in range(2):
            detector.record(exc)

        calls = []

        async def fn():
            calls.append(1)
            raise exc

        policy = RetryPolicy(
            max_attempts=5,
            base_delay_s=0,
            loop_detector=detector,
            jitter=False,
        )
        with pytest.raises(RuntimeError):
            await retry_async(policy, fn)
        # Should have aborted after first failure (because that's the 3rd record)
        assert len(calls) == 1

    async def test_on_retry_callback_invoked(self):
        retry_log = []

        async def on_retry(attempt, exc, delay):
            retry_log.append((attempt, type(exc).__name__))

        async def fn():
            raise RuntimeError("fail")

        policy = RetryPolicy(
            max_attempts=3,
            base_delay_s=0,
            jitter=False,
            on_retry=on_retry,
        )
        with pytest.raises(RuntimeError):
            await retry_async(policy, fn)

        assert len(retry_log) == 2  # called for attempts 1 and 2 (not on final failure)
        assert retry_log[0] == (1, "RuntimeError")

    async def test_only_retryable_exceptions_retried(self):
        calls = []

        async def fn():
            calls.append(1)
            raise ValueError("not retryable")

        policy = RetryPolicy(
            max_attempts=3,
            base_delay_s=0,
            retryable_exceptions=(RuntimeError,),
        )
        with pytest.raises(ValueError):
            await retry_async(policy, fn)
        # ValueError is not retryable — should fail immediately
        assert len(calls) == 1

    async def test_emits_retry_attempt_event_on_bus(self):
        emitted: list[str] = []

        class FakeBus:
            async def emit(self, event):
                emitted.append(event.event_type)

        calls = []

        async def fn():
            calls.append(1)
            if len(calls) < 2:
                raise RuntimeError("transient")
            return "ok"

        policy = RetryPolicy(max_attempts=2, base_delay_s=0, jitter=False)
        await retry_async(policy, fn, bus=FakeBus(), source="test")
        assert "retry.attempt" in emitted

    async def test_emits_loop_detected_event_on_bus(self):
        emitted: list[str] = []

        class FakeBus:
            async def emit(self, event):
                emitted.append(event.event_type)

        detector = ErrorLoopDetector()
        exc = RuntimeError("loop")
        for _ in range(2):
            detector.record(exc)

        async def fn():
            raise exc

        policy = RetryPolicy(
            max_attempts=5, base_delay_s=0, loop_detector=detector, jitter=False
        )
        with pytest.raises(RuntimeError):
            await retry_async(policy, fn, bus=FakeBus(), source="test")
        assert "retry.loop_detected" in emitted
