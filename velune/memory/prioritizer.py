"""Memory Prioritization & Decay Engine.

Calculates memory importance and decay rates using exponential decay
models combined with usage-frequency boosts.
"""

from __future__ import annotations

import math
import time


class MemoryPrioritizer:
    """Manages importance calculations and temporal decay profiles for cognitive memories."""

    def __init__(
        self,
        default_halflife_hours: float = 24.0,
        retrieval_boost_factor: float = 0.15,
        max_importance: float = 1.0,
    ) -> None:
        """
        Initialize the prioritizer.

        - default_halflife_hours: Hours for memory importance to decay by half.
        - retrieval_boost_factor: Percentage boost added to importance on access.
        """
        self.default_halflife_hours = default_halflife_hours
        self.retrieval_boost_factor = retrieval_boost_factor
        self.max_importance = max_importance

        # Compute decay constant (lambda = ln(2) / halflife)
        # Convert halflife from hours to seconds
        self.decay_constant = math.log(2.0) / (default_halflife_hours * 3600.0)

    def calculate_decayed_score(self, initial_score: float, creation_timestamp: float) -> float:
        """
        Applies exponential decay over elapsed time.

        S(t) = S_0 * e^(-lambda * t)
        """
        elapsed_seconds = max(0.0, time.time() - creation_timestamp)
        decayed_score = initial_score * math.exp(-self.decay_constant * elapsed_seconds)
        return max(0.0, min(self.max_importance, decayed_score))

    def calculate_initial_importance(
        self,
        base_importance: float,
        semantic_depth: float = 0.5,
        context_fit: float = 0.5,
    ) -> float:
        """
        Combines baseline importance with dynamic context factors.

        Initial = 0.5 * base + 0.3 * semantic_depth + 0.2 * context_fit
        """
        score = (0.5 * base_importance) + (0.3 * semantic_depth) + (0.2 * context_fit)
        return max(0.0, min(self.max_importance, score))

    def apply_retrieval_boost(self, current_score: float) -> float:
        """
        Apply a retrieval boost when a node is fetched/replayed.
        """
        boosted_score = current_score + (self.retrieval_boost_factor * (self.max_importance - current_score))
        return max(0.0, min(self.max_importance, boosted_score))
