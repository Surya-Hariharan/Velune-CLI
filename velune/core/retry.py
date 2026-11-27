"""Exponential-backoff retry utility with bus event emission."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

T = TypeVar("T")
_log = logging.getLogger("velune.core.retry")


@dataclass
class RetryPolicy:
    """Configuration for retry behaviour passed to :func:`retry_async`."""

    max_attempts: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    jitter: bool = True
    retryable_exceptions: tuple[type[BaseException], ...] = field(
        default_factory=lambda: (Exception,)
    )
    on_retry: Callable[[int, BaseException, float], Awaitable[None]] | None = None
    # Longest provider-supplied wait (``Retry-After``) we will sit through. Beyond this the
    # failure is surfaced immediately: a 429 that says "come back in 10 minutes" (an exhausted
    # quota, not a blip) cannot be waited out inside a request, and sleeping on it only holds
    # a slot and outlasts the caller's own timeout.
    max_retry_after_s: float = 30.0

    def delay_for(self, exc: BaseException, attempt: int) -> float | None:
        """Seconds to wait before retrying after *exc*, or ``None`` to give up now.

        A provider-supplied ``retry_after`` knows better than blind exponential backoff how
        long to wait, but is only honoured up to :attr:`max_retry_after_s`.
        """
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is None:
            return self._delay(attempt)
        try:
            wait = max(0.0, float(retry_after))
        except (TypeError, ValueError):
            return self._delay(attempt)
        return wait if wait <= self.max_retry_after_s else None

    def _delay(self, attempt: int) -> float:
        """Exponential backoff with optional jitter for attempt N (1-based)."""
        delay = min(self.base_delay_s * (2 ** (attempt - 1)), self.max_delay_s)
        if self.jitter:
            delay *= 0.5 + random.random() * 0.5
        return delay


async def retry_async(
    policy: RetryPolicy,
    fn: Callable[[], Awaitable[T]],
    *,
    bus: Any | None = None,
    source: str = "retry",
) -> T:
    """Call *fn* up to *policy.max_attempts* times with exponential backoff.

    - Never retries :exc:`asyncio.CancelledError`.
    - Emits ``retry.attempt`` events on *bus* so the UI can show live status.
    """
    last_exc: BaseException | None = None

    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except policy.retryable_exceptions as exc:
            last_exc = exc

            if attempt >= policy.max_attempts:
                break

            # A RateLimitError (or any exception carrying a provider-supplied
            # `retry_after`, e.g. parsed from an HTTP Retry-After header)
            # knows better than blind exponential backoff how long to wait.
            delay = policy.delay_for(exc, attempt)
            if delay is None:
                _log.warning(
                    "Not retrying: provider asked for a wait of %ss (limit %ss)",
                    getattr(exc, "retry_after", None),
                    policy.max_retry_after_s,
                )
                break

            if bus is not None:
                await _emit(
                    bus,
                    "retry.attempt",
                    source,
                    {
                        "attempt": attempt,
                        "max_attempts": policy.max_attempts,
                        "delay_s": round(delay, 2),
                        "error_type": type(exc).__name__,
                        "error_preview": str(exc)[:100],
                    },
                )

            if policy.on_retry is not None:
                try:
                    await policy.on_retry(attempt, exc, delay)
                except Exception as cb_exc:
                    _log.warning("on_retry callback raised: %s", cb_exc)

            _log.warning(
                "Retry %d/%d after %.1fs (%s: %s)",
                attempt,
                policy.max_attempts - 1,
                delay,
                type(exc).__name__,
                exc,
            )
            await asyncio.sleep(delay)

    assert last_exc is not None
    raise last_exc


async def _emit(bus: Any, event_type: str, source: str, data: dict) -> None:
    """Emit an event on *bus*, swallowing any errors so retry logic is never broken."""
    try:
        from velune.events import Event

        await bus.emit(Event(event_type=event_type, source=source, data=data))
    except Exception as exc:
        _log.debug("Failed to emit %s event: %s", event_type, exc)
