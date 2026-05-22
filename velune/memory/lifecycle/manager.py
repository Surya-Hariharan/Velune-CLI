"""Memory lifecycle orchestrator."""

from typing import Optional
from pathlib import Path
from velune.memory.working.store import WorkingMemoryStore
from velune.memory.working.manager import WorkingMemoryManager
from velune.memory.episodic.store import EpisodicMemoryStore
from velune.memory.semantic.store import SemanticMemoryStore
from velune.memory.procedural.store import ProceduralMemoryStore
from velune.memory.graph.store import GraphMemoryStore
from velune.memory.consolidator.pipeline import ConsolidationPipeline
from velune.memory.consolidator.pruner import MemoryPruner


class MemoryLifecycleManager:
    """Orchestrates memory lifecycle operations."""

    def __init__(self, base_path: Path):
        self.base_path = base_path
        self.base_path.mkdir(parents=True, exist_ok=True)
        
        # Initialize stores
        self.working_store = WorkingMemoryStore()
        self.working_manager = WorkingMemoryManager(self.working_store)
        
        self.episodic_store = EpisodicMemoryStore(
            self.base_path / "episodic.db"
        )
        
        self.semantic_store = SemanticMemoryStore()
        
        self.procedural_store = ProceduralMemoryStore(
            self.base_path / "procedural"
        )
        
        self.graph_store = GraphMemoryStore()
        
        # Initialize consolidator
        self.consolidator = ConsolidationPipeline(
            self.working_store,
            self.episodic_store,
            self.semantic_store,
        )
        
        # Initialize pruner
        self.pruner = MemoryPruner(self.base_path / "archive")

    async def initialize(self) -> None:
        """Initialize the memory lifecycle manager."""
        # Cleanup expired episodic memories
        self.episodic_store.cleanup_expired()

    async def shutdown(self) -> None:
        """Shutdown the memory lifecycle manager."""
        # Run consolidation before shutdown
        await self.consolidator.run_full_consolidation()
        
        # Close episodic store
        self.episodic_store.close()

    def get_working_manager(self) -> WorkingMemoryManager:
        """Get the working memory manager."""
        return self.working_manager

    def get_episodic_store(self) -> EpisodicMemoryStore:
        """Get the episodic memory store."""
        return self.episodic_store

    def get_semantic_store(self) -> SemanticMemoryStore:
        """Get the semantic memory store."""
        return self.semantic_store

    def get_procedural_store(self) -> ProceduralMemoryStore:
        """Get the procedural memory store."""
        return self.procedural_store

    def get_graph_store(self) -> GraphMemoryStore:
        """Get the graph memory store."""
        return self.graph_store

    async def consolidate(self) -> dict[str, int]:
        """Run memory consolidation."""
        return await self.consolidator.run_full_consolidation()
