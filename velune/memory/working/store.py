"""In-process working memory store."""

import time
from typing import Dict, Optional, Any
from velune.core.types import MemoryRecord, MemoryType
from velune.core.errors import MemoryStoreError


class WorkingMemoryStore:
    """In-process working memory for current task state."""

    def __init__(self, ttl_seconds: int = 3600):
        self.ttl_seconds = ttl_seconds
        self._store: Dict[str, MemoryRecord] = {}
        self._task_context: Dict[str, Any] = {}

    def add(self, record: MemoryRecord) -> None:
        """Add a record to working memory."""
        if record.memory_type != MemoryType.WORKING:
            raise MemoryStoreError("Working memory store only accepts WORKING type records")
        
        self._store[record.id] = record

    def get(self, record_id: str) -> Optional[MemoryRecord]:
        """Get a record from working memory."""
        record = self._store.get(record_id)
        if record:
            # Check TTL
            if time.time() - record.created_at.timestamp() > self.ttl_seconds:
                self._store.pop(record_id, None)
                return None
            # Update access count
            record.access_count += 1
            record.last_accessed = record.last_accessed
        return record

    def remove(self, record_id: str) -> None:
        """Remove a record from working memory."""
        self._store.pop(record_id, None)

    def list_all(self) -> list[MemoryRecord]:
        """List all records in working memory."""
        # Filter expired records
        current_time = time.time()
        expired_ids = [
            rid for rid, record in self._store.items()
            if current_time - record.created_at.timestamp() > self.ttl_seconds
        ]
        for rid in expired_ids:
            self._store.pop(rid, None)
        
        return list(self._store.values())

    def clear(self) -> None:
        """Clear all records from working memory."""
        self._store.clear()
        self._task_context.clear()

    def set_task_context(self, key: str, value: Any) -> None:
        """Set a task context value."""
        self._task_context[key] = value

    def get_task_context(self, key: str) -> Optional[Any]:
        """Get a task context value."""
        return self._task_context.get(key)

    def get_task_context_all(self) -> Dict[str, Any]:
        """Get all task context."""
        return self._task_context.copy()
