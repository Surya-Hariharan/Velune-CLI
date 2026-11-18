"""Idle backoff for background polling loops. Standard library only, so any module can import it."""

from __future__ import annotations

IDLE_TICKS_BEFORE_BACKOFF = 5


def backoff_delay(
    base: float, idle_ticks: int, ceiling: float, after: int = IDLE_TICKS_BEFORE_BACKOFF
) -> float:
    """Seconds to wait before the next poll after *idle_ticks* consecutive uneventful ones.

    Returns *base* until *after* idle ticks have passed, then doubles every *after* ticks, never
    above *ceiling* (or *base* itself when that is already larger). Callers reset *idle_ticks*
    to 0 on the first event so polling snaps back to the base rate.
    """
    if idle_ticks < after:
        return base
    doublings = min((idle_ticks - after) // after + 1, 16)  # 16 only bounds the shift
    return min(base * (2**doublings), max(base, ceiling))
