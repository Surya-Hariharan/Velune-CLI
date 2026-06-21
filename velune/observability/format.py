"""Tiny pure formatters shared by the observability commands.

Kept dependency-free and deterministic so they can be unit-tested without a
terminal and reused by both ``velune context`` and ``velune trace``.
"""

from __future__ import annotations

import time


def human_bytes(n: int) -> str:
    """Render a byte count compactly: 0 -> '0 B', 1536 -> '1.5 KB', 5e6 -> '4.8 MB'."""
    if n < 1024:
        return f"{n} B"
    value = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024.0
        if value < 1024.0:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} PB"


def relative_time(ts: float | None, *, now: float | None = None) -> str:
    """Render a unix timestamp as a coarse relative age: 'just now', '5m ago', '3d ago'.

    Returns 'never' for a missing/zero timestamp. ``now`` is injectable so tests
    stay deterministic.
    """
    if not ts:
        return "never"
    current = time.time() if now is None else now
    delta = current - ts
    if delta < 0:
        return "just now"
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"
