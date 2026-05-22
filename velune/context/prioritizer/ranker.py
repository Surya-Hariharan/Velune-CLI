"""Multi-signal context ranking."""

from typing import Dict, Any
from velune.core.types import ContextChunk


class ContextRanker:
    """Ranks context chunks using multiple signals."""

    def __init__(self):
        # Signal weights
        self.weights = {
            "text_similarity": 0.4,
            "recency": 0.2,
            "source_importance": 0.2,
            "length_score": 0.1,
            "metadata_signals": 0.1,
        }

    def rank_chunk(self, chunk: ContextChunk, signals: Dict[str, Any]) -> float:
        """Rank a context chunk based on signals."""
        score = 0.0
        
        for signal_name, weight in self.weights.items():
            if signal_name in signals:
                score += signals[signal_name] * weight
        
        # Incorporate existing relevance score
        score = (score + chunk.relevance_score) / 2
        
        return min(score, 1.0)

    def set_weight(self, signal_name: str, weight: float) -> None:
        """Set weight for a signal."""
        if 0 <= weight <= 1:
            self.weights[signal_name] = weight

    def get_weights(self) -> Dict[str, float]:
        """Get current signal weights."""
        return self.weights.copy()
