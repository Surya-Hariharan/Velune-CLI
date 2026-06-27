"""In-process cache state: fingerprint registry and invalidation tracking."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger("velune.context.cache.state")

_DEBUG = os.environ.get("VELUNE_DEBUG_CACHE", "").lower() in ("1", "true", "yes")


@dataclass
class CacheState:
    """Tracks fingerprints of stable segments and cache hit/miss/write counts.

    One instance should be held per agent (or shared across agents in a single
    council run — same repo context means same fingerprint).
    """

    fingerprints: dict[str, str] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0
    writes: int = 0
    cached_input_tokens: int = 0
    cache_read_tokens: int = 0

    # ------------------------------------------------------------------ #
    # Fingerprint management
    # ------------------------------------------------------------------ #

    def is_valid(self, segment: str, fingerprint: str) -> bool:
        """Return True when *fingerprint* matches the stored value for *segment*.

        A new segment (never seen before) is NOT valid — it must be written.
        """
        return self.fingerprints.get(segment) == fingerprint

    def update(self, segment: str, fingerprint: str, *, action: str) -> None:
        """Store *fingerprint* for *segment* and log the cache action."""
        self.fingerprints[segment] = fingerprint
        if action == "write":
            self.writes += 1
        elif action == "hit":
            self.hits += 1
        else:
            self.misses += 1
        if _DEBUG:
            logger.debug("[cache] %s segment=%r fp=%s", action.upper(), segment, fingerprint)

    def record_tokens(self, creation: int, reads: int) -> None:
        """Accumulate cache token counts from a provider response."""
        self.cached_input_tokens += creation
        self.cache_read_tokens += reads

    # ------------------------------------------------------------------ #
    # Invalidation
    # ------------------------------------------------------------------ #

    def invalidate(self, reason: str = "explicit") -> None:
        """Clear all stored fingerprints.

        Called when workspace, branch, config, provider, or plugin metadata
        changes, ensuring stale prefixes are never reused.
        """
        if _DEBUG:
            logger.debug("[cache] INVALIDATE reason=%r (cleared %d fingerprints)", reason, len(self.fingerprints))
        self.fingerprints.clear()
