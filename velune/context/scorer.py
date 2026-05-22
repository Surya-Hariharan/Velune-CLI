"""Context Scorer for prioritizing information chunks."""

from __future__ import annotations

import time
from typing import Any, Dict, List


class ContextScorer:
    """Computes multidimensional relevance scores for context items to prioritize token inclusion."""

    def __init__(
        self,
        w_recency: float = 0.3,
        w_semantic: float = 0.5,
        w_dependency: float = 0.2,
    ) -> None:
        """
        Initialize weights.
        
        - w_recency: Recency weight (0.0 to 1.0)
        - w_semantic: Semantic search similarity weight (0.0 to 1.0)
        - w_dependency: Call graph/dependency depth weight (0.0 to 1.0)
        """
        self.w_recency = w_recency
        self.w_semantic = w_semantic
        self.w_dependency = w_dependency

    def score_item(
        self,
        semantic_score: float,
        last_accessed_timestamp: float,
        dependency_connections: int = 0,
    ) -> float:
        """
        Calculate total relevance score (0.0 - 1.0) for a piece of context.
        """
        # 1. Recency factor: Decay based on elapsed time (half-life of 2 hours)
        elapsed_hours = (time.time() - last_accessed_timestamp) / 3600.0
        recency_score = 1.0 / (1.0 + elapsed_hours)

        # 2. Semantic score is assumed to be between 0.0 and 1.0 already
        semantic_score = max(0.0, min(1.0, semantic_score))

        # 3. Dependency weight: Logarithmic growth capped at 1.0
        dep_score = min(1.0, dependency_connections / 10.0) if dependency_connections > 0 else 0.0

        # Weighted aggregate
        total_score = (
            self.w_recency * recency_score +
            self.w_semantic * semantic_score +
            self.w_dependency * dep_score
        )

        return max(0.0, min(1.0, total_score))

    def rank_items(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Scores and ranks a list of items.
        Each item dict should optionally contain:
        - 'semantic_score': float
        - 'timestamp': float
        - 'connections': int
        """
        scored_items = []
        for item in items:
            s_score = item.get("semantic_score", 0.5)
            t_stamp = item.get("timestamp", time.time())
            conns = item.get("connections", 0)

            score = self.score_item(
                semantic_score=s_score,
                last_accessed_timestamp=t_stamp,
                dependency_connections=conns,
            )
            
            # Save score into item copy
            item_copy = dict(item)
            item_copy["relevance_score"] = score
            scored_items.append(item_copy)

        # Sort descending by relevance score
        return sorted(scored_items, key=lambda x: x["relevance_score"], reverse=True)
