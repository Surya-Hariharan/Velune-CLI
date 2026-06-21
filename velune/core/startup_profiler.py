"""Lightweight startup phase profiler.

Enable with ``VELUNE_PROFILE_STARTUP=1`` to print a per-phase timing breakdown
of the boot path to stderr. Each ``mark()`` prints immediately (with the delta
since the previous mark), so the breakdown survives even if startup crashes
mid-phase — invaluable for diagnosing where the time goes on a given machine.

Zero overhead when disabled: ``mark()`` short-circuits on the first line.
"""

from __future__ import annotations

import atexit
import os
import sys
import time

_ENABLED = os.environ.get("VELUNE_PROFILE_STARTUP", "").lower() in ("1", "true", "yes")
_PROCESS_START = time.perf_counter()
_marks: list[tuple[str, float]] = []
_reported = False


def enabled() -> bool:
    return _ENABLED


def mark(label: str) -> None:
    """Record and print a startup phase boundary (no-op unless enabled)."""
    if not _ENABLED:
        return
    now = time.perf_counter()
    prev = _marks[-1][1] if _marks else _PROCESS_START
    _marks.append((label, now))
    delta = now - prev
    elapsed = now - _PROCESS_START
    print(
        f"[velune startup] +{elapsed:6.3f}s  Δ{delta:6.3f}s  {label}",
        file=sys.stderr,
        flush=True,
    )


def report() -> None:
    """Print a final total. Registered via atexit; safe to call directly."""
    global _reported
    if not _ENABLED or _reported:
        return
    _reported = True
    total = time.perf_counter() - _PROCESS_START
    print(f"[velune startup] ── total {total:.3f}s ──", file=sys.stderr, flush=True)


if _ENABLED:
    atexit.register(report)
    mark("interpreter -> profiler import")
