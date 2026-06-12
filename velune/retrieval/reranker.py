"""Cross-encoder style reranking (simple scoring for Phase 2a)."""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("velune.retrieval.reranker")


class CrossEncoderReranker:
    """Reranks retrieved chunks using combined scoring.

    Phase 2a: Simple score formula (no ML model)
    Phase 3+: Real cross-encoder when compute available
    """

    # Score weights
    SEMANTIC_WEIGHT = 0.5
    RECENCY_WEIGHT = 0.3
    TRUST_WEIGHT = 0.2

    # Thresholds
    MIN_RECENCY_SECONDS = 300
    MAX_RECENCY_SECONDS = 2592000

    def __init__(self) -> None:
        """Initialize reranker."""
        pass

    def rerank(
        self,
        chunks: list[Any],
        query: str | None = None,
    ) -> list[Any]:
        """Rerank chunks by combined score."""
        start_time = time.perf_counter()

        for chunk in chunks:
            chunk.combined_score = self._calculate_combined_score(chunk)

        ranked = sorted(chunks, key=lambda c: c.combined_score, reverse=True)
        deduplicated = self._deduplicate_by_content(ranked)

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        logger.debug(f"Reranking {len(chunks)} → {len(deduplicated)} in {elapsed_ms:.1f}ms")

        return deduplicated

    def _calculate_combined_score(self, chunk: Any) -> float:
        """Calculate combined score."""
        semantic = min(1.0, max(0.0, chunk.relevance_score or 0.0))
        recency = self._calculate_recency_score(chunk)
        trust = self._calculate_trust_score(chunk)

        combined = (
            (semantic * self.SEMANTIC_WEIGHT) +
            (recency * self.RECENCY_WEIGHT) +
            (trust * self.TRUST_WEIGHT)
        )

        return min(1.0, max(0.0, combined))

    def _calculate_recency_score(self, chunk: Any) -> float:
        """Calculate recency score."""
        timestamp = chunk.metadata.get("timestamp")
        if not timestamp:
            return 0.5

        now = time.time()
        age_seconds = now - timestamp

        if age_seconds < self.MIN_RECENCY_SECONDS:
            return 1.0
        if age_seconds > self.MAX_RECENCY_SECONDS:
            return 0.0

        age_range = self.MAX_RECENCY_SECONDS - self.MIN_RECENCY_SECONDS
        score = 1.0 - ((age_seconds - self.MIN_RECENCY_SECONDS) / age_range)
        return min(1.0, max(0.0, score))

    def _calculate_trust_score(self, chunk: Any) -> float:
        """Calculate trust score based on source."""
        source_trust = {
            "symbol": 0.9,
            "import_graph": 0.85,
            "lineage": 0.8,
            "call_graph": 0.75,
            "episodic": 0.7,
            "semantic": 0.6,
        }
        return source_trust.get(chunk.source, 0.5)

    def _deduplicate_by_content(self, chunks: list[Any]) -> list[Any]:
        """Remove similar chunks keeping highest score."""
        seen: dict[str, Any] = {}
        for chunk in chunks:
            content_sig = chunk.content[:100].lower()
            if content_sig not in seen or chunk.combined_score > seen[content_sig].combined_score:
                seen[content_sig] = chunk
        return list(seen.values())
