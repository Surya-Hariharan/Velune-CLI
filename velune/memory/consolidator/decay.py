"""Temporal decay model."""

import time
from typing import float
from velune.core.types import MemoryRecord


class DecayModel:
    """Models temporal decay of memory importance."""

    def __init__(self, half_life_days: float = 30.0):
        self.half_life_days = half_life_days

    def decay(self, record: MemoryRecord) -> float:
        """Apply decay to a memory record's importance."""
        age_days = (time.time() - record.created_at.timestamp()) / 86400
        
        # Exponential decay
        decay_factor = 0.5 ** (age_days / self.half_life_days)
        
        return record.importance * decay_factor

    def should_prune(self, record: MemoryRecord, threshold: float = 0.1) -> bool:
        """Check if a record should be pruned based on decay."""
        decayed_importance = self.decay(record)
        return decayed_importance < threshold
