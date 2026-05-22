"""Priority scoring engine."""

from typing import Dict, Any
from velune.core.types import ContextChunk, ContextPriority
from velune.context.prioritizer.signals import SignalExtractor
from velune.context.prioritizer.ranker import ContextRanker


class PriorityEngine:
    """Engine for scoring context chunk priority."""

    def __init__(self):
        self.signal_extractor = SignalExtractor()
        self.ranker = ContextRanker()

    def score_chunk(self, chunk: ContextChunk, query: str) -> float:
        """Score a context chunk's priority."""
        signals = self.signal_extractor.extract_signals(chunk, query)
        return self.ranker.rank_chunk(chunk, signals)

    def score_chunks(
        self,
        chunks: list[ContextChunk],
        query: str,
    ) -> list[tuple[ContextChunk, float]]:
        """Score multiple context chunks."""
        scored = []
        for chunk in chunks:
            score = self.score_chunk(chunk, query)
            scored.append((chunk, score))
        return scored

    def assign_priority(
        self,
        chunk: ContextChunk,
        score: float,
    ) -> ContextPriority:
        """Assign priority level based on score."""
        if score >= 0.8:
            return ContextPriority.CRITICAL
        elif score >= 0.6:
            return ContextPriority.HIGH
        elif score >= 0.4:
            return ContextPriority.MEDIUM
        else:
            return ContextPriority.LOW
