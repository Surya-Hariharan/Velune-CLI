"""Memory importance scoring."""

from typing import float
from velune.core.types import MemoryRecord


class ImportanceScorer:
    """Scores memory importance."""

    def score(self, record: MemoryRecord) -> float:
        """Score the importance of a memory record."""
        score = 0.5  # Base score
        
        # Access count factor
        score += min(record.access_count * 0.05, 0.3)
        
        # Recency factor (more recent = more important)
        import time
        days_old = (time.time() - record.created_at.timestamp()) / 86400
        score += max(0, 0.3 - days_old * 0.01)
        
        # Content length factor (longer = potentially more important)
        if len(record.content) > 100:
            score += 0.1
        if len(record.content) > 500:
            score += 0.1
        
        return min(score, 1.0)
