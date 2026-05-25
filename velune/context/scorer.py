"""Context Scorer for prioritizing information chunks."""

from __future__ import annotations

import math
import time
from typing import Any

from velune.memory.prioritizer import MemoryPrioritizer


class ContextAttentionPrioritizer(MemoryPrioritizer):
    """
    Advanced attention prioritizer that calculates context importance scores.
    Features:
    - Type-based temporal decay profiles
    - Unresolved task boosts (+0.4)
    - Call-graph depth / core dependency boosts
    """

    def __init__(
        self,
        default_halflife_hours: float = 24.0,
        retrieval_boost_factor: float = 0.15,
        max_importance: float = 1.0,
        type_halflives: dict[str, float] | None = None,
    ) -> None:
        super().__init__(
            default_halflife_hours=default_halflife_hours,
            retrieval_boost_factor=retrieval_boost_factor,
            max_importance=max_importance,
        )
        self.type_halflives = type_halflives or {
            "source_code": 48.0,
            "execution_log": 2.0,
            "log": 2.0,
            "task": 168.0,
            "error": 72.0,
        }

    def score_context_item(
        self,
        base_score: float,
        last_accessed_timestamp: float,
        context_type: str = "general",
        is_unresolved_task: bool = False,
        dependency_connections: int = 0,
        is_core_dependency: bool = False,
    ) -> float:
        """
        Calculate the attention importance score for a context item.
        """
        # 1. Type-based temporal decay using memory prioritizer logic
        halflife = self.type_halflives.get(context_type, self.default_halflife_hours)
        decay_const = math.log(2.0) / (halflife * 3600.0)
        elapsed_seconds = max(0.0, time.time() - last_accessed_timestamp)
        decayed = base_score * math.exp(-decay_const * elapsed_seconds)
        score = max(0.0, min(self.max_importance, decayed))

        # 2. Unresolved Task Boost (+0.4)
        if is_unresolved_task:
            score += 0.4

        # 3. Call-Graph Depth / Dependency Boost
        if dependency_connections > 0:
            # Logarithmic connection boost capped at 0.2
            dep_boost = min(0.2, math.log10(dependency_connections + 1) * 0.1)
            score += dep_boost

        if is_core_dependency:
            score += 0.15

        return max(0.0, min(self.max_importance, score))


class ContextScorer:
    """Computes multidimensional relevance scores for context items to prioritize token inclusion."""

    def __init__(
        self,
        w_recency: float = 0.3,
        w_semantic: float = 0.5,
        w_dependency: float = 0.2,
        prioritizer: ContextAttentionPrioritizer | None = None,
    ) -> None:
        """
        Initialize weights and the underlying advanced prioritizer.
        
        - w_recency: Recency weight (0.0 to 1.0)
        - w_semantic: Semantic search similarity weight (0.0 to 1.0)
        - w_dependency: Call graph/dependency depth weight (0.0 to 1.0)
        """
        self.w_recency = w_recency
        self.w_semantic = w_semantic
        self.w_dependency = w_dependency
        self.prioritizer = prioritizer or ContextAttentionPrioritizer()

    def score_item(
        self,
        semantic_score: float,
        last_accessed_timestamp: float,
        dependency_connections: int = 0,
        context_type: str = "general",
        is_unresolved_task: bool = False,
        is_core_dependency: bool = False,
    ) -> float:
        """
        Calculate total relevance score (0.0 - 1.0) for a piece of context.
        """
        # Get advanced attention score
        attention_score = self.prioritizer.score_context_item(
            base_score=semantic_score,
            last_accessed_timestamp=last_accessed_timestamp,
            context_type=context_type,
            is_unresolved_task=is_unresolved_task,
            dependency_connections=dependency_connections,
            is_core_dependency=is_core_dependency,
        )

        # Blend prioritizer attention score with traditional weighted calculation
        elapsed_hours = (time.time() - last_accessed_timestamp) / 3600.0
        recency_score = 1.0 / (1.0 + elapsed_hours)
        semantic_score = max(0.0, min(1.0, semantic_score))
        dep_score = min(1.0, dependency_connections / 10.0) if dependency_connections > 0 else 0.0

        traditional_score = (
            self.w_recency * recency_score +
            self.w_semantic * semantic_score +
            self.w_dependency * dep_score
        )

        # We blend the traditional score (50%) and the advanced prioritizer score (50%)
        # But if is_unresolved_task is True, we ensure the boost propagates strongly.
        total_score = 0.5 * traditional_score + 0.5 * attention_score
        return max(0.0, min(1.0, total_score))

    def rank_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Scores and ranks a list of items.
        Each item dict should optionally contain:
        - 'semantic_score': float
        - 'timestamp': float
        - 'connections': int
        - 'context_type': str
        - 'is_unresolved_task': bool
        - 'is_core_dependency': bool
        """
        scored_items = []
        for item in items:
            s_score = item.get("semantic_score", 0.5)
            t_stamp = item.get("timestamp", time.time())
            conns = item.get("connections", 0)
            c_type = item.get("context_type", "general")
            unresolved = item.get("is_unresolved_task", False)
            core_dep = item.get("is_core_dependency", False)

            score = self.score_item(
                semantic_score=s_score,
                last_accessed_timestamp=t_stamp,
                dependency_connections=conns,
                context_type=c_type,
                is_unresolved_task=unresolved,
                is_core_dependency=core_dep,
            )

            # Save score into item copy
            item_copy = dict(item)
            item_copy["relevance_score"] = score
            scored_items.append(item_copy)

        # Sort descending by relevance score
        return sorted(scored_items, key=lambda x: x["relevance_score"], reverse=True)
