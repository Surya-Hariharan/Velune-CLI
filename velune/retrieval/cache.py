"""In-process retrieval cache with LRU eviction."""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger("velune.retrieval.cache")


class RetrievalCache:
    """In-process cache for retrieval results with LRU eviction."""

    # Configuration
    MAX_ENTRIES = 200
    TTL_SECONDS = 300  # 5 minutes

    def __init__(self) -> None:
        """Initialize retrieval cache."""
        self._cache: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._hits = 0
        self._misses = 0

    async def get(self, key: str) -> Any | None:
        """Get cached result.

        Parameters
        ----------
        key:
            Cache key

        Returns
        -------
        RetrievedContext | None:
            Cached result or None if not found/expired
        """
        if key not in self._cache:
            self._misses += 1
            return None

        result, timestamp = self._cache[key]

        # Check TTL
        if time.time() - timestamp > self.TTL_SECONDS:
            del self._cache[key]
            self._misses += 1
            return None

        # Move to end (LRU)
        self._cache.move_to_end(key)
        self._hits += 1

        logger.debug(f"Cache hit (rate: {self.hit_rate():.1%})")
        return result

    async def set(self, key: str, result: Any) -> None:
        """Set cache entry.

        Parameters
        ----------
        key:
            Cache key
        result:
            Result to cache
        """
        # Evict LRU if needed
        if len(self._cache) >= self.MAX_ENTRIES:
            evicted_key, _ = self._cache.popitem(last=False)
            logger.debug(f"Cache evicted LRU entry: {evicted_key[:8]}...")

        self._cache[key] = (result, time.time())
        logger.debug(f"Cached result (entries: {len(self._cache)})")

    def clear(self) -> None:
        """Clear all cache entries."""
        self._cache.clear()
        logger.info("Cache cleared")

    def hit_rate(self) -> float:
        """Get cache hit rate.

        Returns
        -------
        float:
            Hit rate (0.0 to 1.0)
        """
        total = self._hits + self._misses
        if total == 0:
            return 0.0
        return self._hits / total

    def stats(self) -> dict[str, Any]:
        """Get cache statistics.

        Returns
        -------
        dict:
            Cache stats
        """
        total = self._hits + self._misses
        return {
            "entries": len(self._cache),
            "max_entries": self.MAX_ENTRIES,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": self.hit_rate(),
            "total_requests": total,
        }
