"""Context reconstruction from memory."""

from typing import list, Optional
from velune.core.types import ContextChunk, ContextWindow, MemoryQuery, MemoryType
from velune.memory.semantic.store import SemanticMemoryStore
from velune.memory.episodic.store import EpisodicMemoryStore


class ContextReconstructor:
    """Reconstructs context from memory."""

    def __init__(
        self,
        semantic_store: SemanticMemoryStore,
        episodic_store: EpisodicMemoryStore,
    ):
        self.semantic_store = semantic_store
        self.episodic_store = episodic_store

    def reconstruct(
        self,
        query: str,
        limit: int = 20,
    ) -> list[ContextChunk]:
        """Reconstruct context from memory based on query."""
        chunks = []
        
        # Query semantic memory
        semantic_query = MemoryQuery(
            query_text=query,
            memory_types=[MemoryType.SEMANTIC],
            limit=limit // 2,
        )
        
        try:
            semantic_records = self.semantic_store.query(semantic_query)
            for record in semantic_records:
                chunks.append(self._record_to_chunk(record))
        except Exception:
            pass
        
        # Query episodic memory
        try:
            episodic_records = self.episodic_store.get_recent(limit // 2)
            for record in episodic_records:
                chunks.append(self._record_to_chunk(record))
        except Exception:
            pass
        
        return chunks

    def _record_to_chunk(self, record) -> ContextChunk:
        """Convert a memory record to a context chunk."""
        from velune.core.types import ContextPriority
        import time
        
        return ContextChunk(
            content=record.content,
            source=f"memory:{record.memory_type.value}",
            priority=ContextPriority.MEDIUM,
            tokens=len(record.content) // 4,
            relevance_score=record.importance,
            timestamp=time.time(),
            metadata={"memory_id": record.id, **record.metadata},
        )
