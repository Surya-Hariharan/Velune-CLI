"""Working to episodic to semantic consolidation pipeline."""

from typing import Optional
from velune.memory.working.store import WorkingMemoryStore
from velune.memory.episodic.store import EpisodicMemoryStore
from velune.memory.semantic.store import SemanticMemoryStore
from velune.memory.episodic.encoder import EpisodicEncoder
from velune.memory.semantic.extractor import FactExtractor
from velune.core.types import MemoryRecord, MemoryType


class ConsolidationPipeline:
    """Pipeline for consolidating memory across types."""

    def __init__(
        self,
        working_store: WorkingMemoryStore,
        episodic_store: EpisodicMemoryStore,
        semantic_store: SemanticMemoryStore,
    ):
        self.working_store = working_store
        self.episodic_store = episodic_store
        self.semantic_store = semantic_store
        self.episodic_encoder = EpisodicEncoder()
        self.fact_extractor = FactExtractor()

    async def consolidate_working_to_episodic(self) -> int:
        """Consolidate working memory to episodic memory."""
        records = self.working_store.list_all()
        consolidated = 0
        
        for record in records:
            # Encode as episodic
            episodic_record = MemoryRecord(
                id=record.id,
                memory_type=MemoryType.EPISODIC,
                content=record.content,
                importance=record.importance,
                access_count=record.access_count,
                last_accessed=record.last_accessed,
                created_at=record.created_at,
                expires_at=None,
                metadata=record.metadata,
            )
            
            self.episodic_store.add(episodic_record)
            consolidated += 1
        
        # Clear working memory after consolidation
        self.working_store.clear()
        
        return consolidated

    async def consolidate_episodic_to_semantic(self) -> int:
        """Consolidate episodic memory to semantic memory."""
        records = self.episodic_store.get_recent(100)
        consolidated = 0
        
        for record in records:
            # Extract facts
            facts = self.fact_extractor.extract_from_documentation(record.content)
            
            for fact in facts:
                # In production, generate embedding here
                semantic_record = MemoryRecord(
                    id=f"{record.id}_fact_{consolidated}",
                    memory_type=MemoryType.SEMANTIC,
                    content=fact,
                    importance=record.importance * 0.8,
                    access_count=0,
                    last_accessed=record.last_accessed,
                    created_at=record.created_at,
                    expires_at=None,
                    metadata={"source": record.id, **record.metadata},
                )
                
                self.semantic_store.add(semantic_record)
                consolidated += 1
        
        return consolidated

    async def run_full_consolidation(self) -> dict[str, int]:
        """Run the full consolidation pipeline."""
        working_to_episodic = await self.consolidate_working_to_episodic()
        episodic_to_semantic = await self.consolidate_episodic_to_semantic()
        
        return {
            "working_to_episodic": working_to_episodic,
            "episodic_to_semantic": episodic_to_semantic,
        }
