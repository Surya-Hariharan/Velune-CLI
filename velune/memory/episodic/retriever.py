"""Temporal and semantic episodic retrieval."""

from typing import list, Optional
from datetime import datetime, timedelta
from velune.memory.episodic.store import EpisodicMemoryStore
from velune.core.types import MemoryRecord, MemoryQuery


class EpisodicRetriever:
    """Retriever for episodic memory."""

    def __init__(self, store: EpisodicMemoryStore):
        self.store = store

    def retrieve_recent(self, limit: int = 10) -> list[MemoryRecord]:
        """Retrieve recent episodic memories."""
        return self.store.get_recent(limit)

    def retrieve_by_time_range(
        self,
        start: datetime,
        end: datetime,
        limit: int = 100,
    ) -> list[MemoryRecord]:
        """Retrieve episodic memories within a time range."""
        # This would require adding time range queries to the store
        # For now, return recent and filter
        records = self.store.get_recent(limit * 2)
        return [
            r for r in records
            if start <= r.created_at <= end
        ][:limit]

    def retrieve_by_importance(
        self,
        min_importance: float = 0.7,
        limit: int = 10,
    ) -> list[MemoryRecord]:
        """Retrieve episodic memories by importance."""
        records = self.store.get_recent(100)
        filtered = [r for r in records if r.importance >= min_importance]
        filtered.sort(key=lambda r: r.importance, reverse=True)
        return filtered[:limit]

    def retrieve_by_query(
        self,
        query: str,
        limit: int = 10,
    ) -> list[MemoryRecord]:
        """Retrieve episodic memories by content query."""
        return self.store.search_by_content(query, limit)

    def retrieve_similar(
        self,
        query_embedding: list[float],
        limit: int = 10,
    ) -> list[MemoryRecord]:
        """Retrieve episodic memories by semantic similarity."""
        # This would require vector similarity search
        # For now, return recent records
        return self.store.get_recent(limit)
