"""Vitality classification for memory retention and retrieval.

Phase 2a: Recency-based vitality states (LIVE/ZOMBIE/ARCHIVED) determine
whether a memory is eligible for inclusion in context retrieval.

Classification is session-relative: a turn's vitality depends on how many
sessions have passed since it was recorded.
"""

from __future__ import annotations

import enum
import logging
import time
from typing import Any

logger = logging.getLogger("velune.memory.vitality")


class Vitality(enum.Enum):
    """Recency-based memory state for retrieval eligibility."""

    LIVE = "live"           # Turn from last 3 sessions; always retrieved
    ZOMBIE = "zombie"       # Turn from 4-10 sessions ago; retrieved only on LIVE miss
    ARCHIVED = "archived"   # Turn > 10 sessions old; never retrieved (unless explicit)


class VitalityClassifier:
    """Classify memory vitality based on session distance and age."""

    def __init__(
        self,
        live_window: int = 3,
        zombie_window: int = 10,
        ttl_seconds: float = 2_592_000,  # 30 days default
    ) -> None:
        """Initialize classifier with vitality thresholds.

        Parameters
        ----------
        live_window:
            Sessions from current: 0-N are LIVE (default 3 sessions).
        zombie_window:
            Sessions from current: (N+1)-M are ZOMBIE; >M are ARCHIVED (default 10).
        ttl_seconds:
            Max age in seconds before forced ARCHIVED (default 30 days).
        """
        self.live_window = live_window
        self.zombie_window = zombie_window
        self.ttl_seconds = ttl_seconds

    def classify_turn(
        self,
        turn: Any,
        current_session_index: int,
        turn_session_index: int,
        now: float | None = None,
    ) -> Vitality:
        """Classify a turn's vitality based on session distance and age.

        Parameters
        ----------
        turn:
            The turn object with a 'created_at' timestamp attribute.
        current_session_index:
            The ordinal index of the current session (0 = oldest).
        turn_session_index:
            The ordinal index of the session containing this turn.
        now:
            Current timestamp; defaults to time.time().

        Returns
        -------
        Vitality:
            One of LIVE, ZOMBIE, ARCHIVED.
        """
        if now is None:
            now = time.time()

        # Session distance: how many sessions old is this turn?
        session_distance = current_session_index - turn_session_index

        # Age-based cutoff: if turn is older than TTL, it's archived.
        age = now - getattr(turn, "created_at", now)
        if age > self.ttl_seconds:
            return Vitality.ARCHIVED

        # Session-based classification.
        if session_distance <= self.live_window:
            return Vitality.LIVE
        if session_distance <= self.zombie_window:
            return Vitality.ZOMBIE
        return Vitality.ARCHIVED

    def should_include(
        self,
        vitality: Vitality,
        fallback_to_zombie: bool = False,
    ) -> bool:
        """Return True if a memory with this vitality should be retrieved.

        Parameters
        ----------
        vitality:
            The turn's vitality state.
        fallback_to_zombie:
            If True and no LIVE results found, include ZOMBIE (not default).

        Returns
        -------
        bool:
            True if the memory is eligible for retrieval.
        """
        if vitality == Vitality.LIVE:
            return True
        if fallback_to_zombie and vitality == Vitality.ZOMBIE:
            return True
        return False

    def decay_factor(self, vitality: Vitality) -> float:
        """Return a trust/confidence decay factor for this vitality.

        LIVE memories have full confidence; ZOMBIE memories are discounted.
        ARCHIVED memories should not appear, but if they do, score them low.
        """
        match vitality:
            case Vitality.LIVE:
                return 1.0
            case Vitality.ZOMBIE:
                return 0.6
            case Vitality.ARCHIVED:
                return 0.2
