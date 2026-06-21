"""Sliding-window error loop detector for retry-aware error self-healing."""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass
class LoopSignal:
    """Result of recording an exception against the detector."""

    fingerprint: str
    occurrences: int
    is_looping: bool
    error_type: str
    error_preview: str
    first_seen: float
    last_seen: float


class ErrorLoopDetector:
    """Detects when the same error repeats too many times within a rolling window.

    Thread-safe — uses no asyncio primitives so it can be called from sync or
    async contexts without restriction.

    A "loop" is declared when the same error fingerprint appears
    ``_LOOP_THRESHOLD`` or more times within ``_WINDOW_SECONDS``.
    """

    _WINDOW_SECONDS: float = 300.0  # 5-minute sliding window
    _LOOP_THRESHOLD: int = 3

    def __init__(self) -> None:
        self._occurrences: dict[str, deque[float]] = defaultdict(deque)
        self._first_seen: dict[str, float] = {}
        self._metadata: dict[str, tuple[str, str]] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fingerprint(self, exc: BaseException) -> str:
        key = f"{type(exc).__name__}:{str(exc)[:100]}"
        return hashlib.sha1(key.encode()).hexdigest()[:16]

    def _evict_old(self, fp: str, now: float) -> None:
        dq = self._occurrences[fp]
        while dq and (now - dq[0]) > self._WINDOW_SECONDS:
            dq.popleft()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, exc: BaseException) -> LoopSignal:
        """Record *exc* and return a :class:`LoopSignal` describing the current state."""
        fp = self._fingerprint(exc)
        now = time.monotonic()
        self._evict_old(fp, now)

        dq = self._occurrences[fp]
        if not dq:
            self._first_seen[fp] = now
        dq.append(now)
        self._metadata[fp] = (type(exc).__name__, str(exc)[:100])

        count = len(dq)
        type_name, preview = self._metadata[fp]
        return LoopSignal(
            fingerprint=fp,
            occurrences=count,
            is_looping=count >= self._LOOP_THRESHOLD,
            error_type=type_name,
            error_preview=preview,
            first_seen=self._first_seen.get(fp, now),
            last_seen=now,
        )

    def is_looping(self, exc: BaseException) -> bool:
        """Return True if *exc* already qualifies as a loop without recording it."""
        fp = self._fingerprint(exc)
        now = time.monotonic()
        self._evict_old(fp, now)
        return len(self._occurrences.get(fp, deque())) >= self._LOOP_THRESHOLD

    def clear(self, fingerprint: str) -> None:
        """Remove all history for *fingerprint* — called when a job is cancelled."""
        self._occurrences.pop(fingerprint, None)
        self._first_seen.pop(fingerprint, None)
        self._metadata.pop(fingerprint, None)

    def clear_all(self) -> None:
        """Reset all loop state."""
        self._occurrences.clear()
        self._first_seen.clear()
        self._metadata.clear()
