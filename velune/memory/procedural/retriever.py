"""Pattern retrieval for similar tasks."""

from typing import list, Optional
from velune.memory.procedural.store import ProceduralMemoryStore
from velune.core.types import MemoryRecord


class ProceduralRetriever:
    """Retriever for procedural memory patterns."""

    def __init__(self, store: ProceduralMemoryStore):
        self.store = store

    def retrieve_for_task(
        self,
        task_description: str,
        limit: int = 5,
    ) -> list[MemoryRecord]:
        """Retrieve procedural patterns for a similar task."""
        return self.store.find_similar(task_description, limit)

    def retrieve_by_importance(
        self,
        min_importance: float = 0.7,
        limit: int = 10,
    ) -> list[MemoryRecord]:
        """Retrieve procedural patterns by importance."""
        patterns = self.store.list_all()
        filtered = [p for p in patterns if p.importance >= min_importance]
        filtered.sort(key=lambda p: p.importance, reverse=True)
        return filtered[:limit]

    def retrieve_all(self) -> list[MemoryRecord]:
        """Retrieve all procedural patterns."""
        return self.store.list_all()
