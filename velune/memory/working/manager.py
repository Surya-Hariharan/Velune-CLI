"""Working memory lifecycle manager."""

from typing import Optional
from velune.memory.working.store import WorkingMemoryStore
from velune.core.types import MemoryRecord


class WorkingMemoryManager:
    """Manager for working memory lifecycle."""

    def __init__(self, store: WorkingMemoryStore):
        self.store = store

    def add_observation(self, content: str, importance: float = 0.5) -> MemoryRecord:
        """Add an observation to working memory."""
        import uuid
        from datetime import datetime
        from velune.core.types import MemoryType
        
        record = MemoryRecord(
            id=str(uuid.uuid4()),
            memory_type=MemoryType.WORKING,
            content=content,
            importance=importance,
            access_count=0,
            last_accessed=datetime.now(),
            created_at=datetime.now(),
        )
        self.store.add(record)
        return record

    def add_thought(self, content: str, importance: float = 0.7) -> MemoryRecord:
        """Add a thought to working memory."""
        return self.add_observation(content, importance)

    def add_action(self, content: str, importance: float = 0.8) -> MemoryRecord:
        """Add an action to working memory."""
        return self.add_observation(content, importance)

    def get_recent(self, limit: int = 10) -> list[MemoryRecord]:
        """Get recent records from working memory."""
        records = self.store.list_all()
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records[:limit]

    def get_important(self, min_importance: float = 0.7) -> list[MemoryRecord]:
        """Get important records from working memory."""
        records = self.store.list_all()
        return [r for r in records if r.importance >= min_importance]

    def clear(self) -> None:
        """Clear working memory."""
        self.store.clear()
