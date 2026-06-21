"""Exponential-backoff retry utility with loop detection and bus event emission."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from velune.core.loop_detector import ErrorLoopDetector

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
    loop_detector: ErrorLoopDetector | None = None
    on_retry: Callable[[int, BaseException, float], Awaitable[None]] | None = None

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
    - Checks :class:`~velune.core.loop_detector.ErrorLoopDetector` before each
      retry; if a loop is detected the detector fires a ``retry.loop_detected``
      event on *bus* and re-raises immediately.
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

            if policy.loop_detector is not None:
                sig = policy.loop_detector.record(exc)
                if sig.is_looping:
                    _log.error(
                        "Loop detected for %s: '%s' seen %d times — aborting",
                        sig.error_type,
                        sig.error_preview,
                        sig.occurrences,
                    )
                    if bus is not None:
                        await _emit(
                            bus,
                            "retry.loop_detected",
                            source,
                            {
                                "fingerprint": sig.fingerprint,
                                "error_type": sig.error_type,
                                "error_preview": sig.error_preview,
                                "occurrences": sig.occurrences,
                            },
                        )
                    raise

            if attempt >= policy.max_attempts:
                break

            delay = policy._delay(attempt)

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
