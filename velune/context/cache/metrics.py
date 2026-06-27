"""Structured cache hit/miss/token/cost statistics."""

from __future__ import annotations

from dataclasses import dataclass, field

# Anthropic cache-read tokens cost ~10% of normal input tokens.
# We use this ratio for estimated savings when we don't have the exact model rate.
_CACHE_READ_DISCOUNT = 0.90  # fraction saved per cached token

# Rough default input cost (USD/1M tokens) used when model rate is unknown.
_DEFAULT_INPUT_COST_PER_M = 3.0  # Sonnet-class baseline


@dataclass
class CacheMetrics:
    """Accumulated cache statistics for a session or a single manager instance."""

    writes: int = 0
    hits: int = 0
    misses: int = 0
    cached_input_tokens: int = 0   # tokens written into the cache (billed at full price)
    cache_read_tokens: int = 0     # tokens read from cache (billed at ~10% price)

    # Per-call tracking (populated by ContextCacheManager.record())
    _model_id: str = field(default="", repr=False)
    _provider_id: str = field(default="", repr=False)

    # ------------------------------------------------------------------ #
    # Derived properties
    # ------------------------------------------------------------------ #

    @property
    def hit_rate(self) -> float:
        """Fraction of requests that were served from cache (0.0–1.0)."""
        total = self.hits + self.misses + self.writes
        return self.hits / total if total else 0.0

    @property
    def estimated_token_savings(self) -> int:
        """Tokens saved by cache reads (i.e., tokens not re-processed as full input)."""
        return self.cache_read_tokens

    @property
    def estimated_cost_savings_usd(self) -> float:
        """Estimated USD saved by cache reads vs billing them at full input price.

        Uses a conservative default rate when the exact model is unknown.
        Callers that have the real rate should compute this themselves using
        ``cache_read_tokens`` and the provider cost table.
        """
        cost_per_token = _DEFAULT_INPUT_COST_PER_M / 1_000_000
        savings_per_cached_token = cost_per_token * _CACHE_READ_DISCOUNT
        return self.cache_read_tokens * savings_per_cached_token

    # ------------------------------------------------------------------ #
    # Merging
    # ------------------------------------------------------------------ #

    def merge(self, other: "CacheMetrics") -> None:
        """Merge another CacheMetrics into this one (in-place)."""
        self.writes += other.writes
        self.hits += other.hits
        self.misses += other.misses
        self.cached_input_tokens += other.cached_input_tokens
        self.cache_read_tokens += other.cache_read_tokens

    def __repr__(self) -> str:
        return (
            f"CacheMetrics(writes={self.writes}, hits={self.hits}, misses={self.misses}, "
            f"cached_input_tokens={self.cached_input_tokens}, cache_read_tokens={self.cache_read_tokens}, "
            f"hit_rate={self.hit_rate:.1%}, est_savings_usd={self.estimated_cost_savings_usd:.4f})"
        )
