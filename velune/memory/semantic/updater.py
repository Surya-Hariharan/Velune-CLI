"""Semantic memory update (merge, supersede)."""

from typing import Optional
from velune.memory.semantic.store import SemanticMemoryStore
from velune.core.types import MemoryRecord, MemoryType


class SemanticUpdater:
    """Updates semantic memory with merge and supersede operations."""

    def __init__(self, store: SemanticMemoryStore):
        self.store = store

    def merge(self, existing: MemoryRecord, new: MemoryRecord) -> MemoryRecord:
        """Merge two semantic memory records."""
        # Combine content
        merged_content = f"{existing.content}\n\n{new.content}"
        
        # Average importance
        merged_importance = (existing.importance + new.importance) / 2
        
        # Keep the newer created_at
        merged_created_at = new.created_at if new.created_at > existing.created_at else existing.created_at
        
        # Merge metadata
        merged_metadata = {**existing.metadata, **new.metadata}
        
        return MemoryRecord(
            id=existing.id,  # Keep existing ID
            memory_type=MemoryType.SEMANTIC,
            content=merged_content,
            embedding=new.embedding or existing.embedding,
            importance=merged_importance,
            access_count=existing.access_count + new.access_count,
            last_accessed=new.last_accessed,
            created_at=merged_created_at,
            expires_at=None,
            metadata=merged_metadata,
        )

    def supersede(self, old_record: MemoryRecord, new_record: MemoryRecord) -> None:
        """Supersede an old record with a new one."""
        # Delete old record
        self.store.delete(old_record.id)
        
        # Add new record
        self.store.add(new_record)

    def update_or_create(self, record: MemoryRecord) -> None:
        """Update existing record or create new one."""
        # Check if similar record exists
        # For now, just add
        self.store.add(record)
