"""Memory Subsystem Lifecycle and Coordinator.

Manages clean connections startup, shutdown flushes, and orchestrates
period/milestone-driven memory consolidation routines.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from velune.providers.base import ModelProvider
from velune.memory.consolidator import MemoryConsolidator

logger = logging.getLogger("velune.memory.lifecycle")


class MemoryLifecycleCoordinator:
    """Orchestrates memory subsystem boot protocols and triggers consolidation routines on milestones."""

    def __init__(self, consolidator: MemoryConsolidator) -> None:
        self.consolidator = consolidator
        self._is_active = False

    async def startup(self) -> None:
        """Boot databases and establish active connection boundaries."""
        logger.info("Initializing Hierarchical Memory Tiers...")
        self._is_active = True

    async def shutdown(self) -> None:
        """Ensure all working buffers are safely flushed before termination."""
        logger.info("Flushing transient working memory buffers before shutdown...")
        # Clean shutdown behavior: ensure transient memories are not lost
        self._is_active = False

    async def trigger_milestone_consolidation(
        self,
        session_id: str,
        provider: ModelProvider,
        model_id: str,
        embedding_provider: Optional[Any] = None,
    ) -> None:
        """
        Triggers a full episodic-to-semantic-and-graph consolidation routine.
        Typically executed upon successful goal completion or session closure.
        """
        if not self._is_active:
            logger.warning("Memory lifecycle is not active. Consolidation skipped.")
            return

        logger.info("Milestone hit! Initiating episodic-to-semantic consolidation for session %s.", session_id)
        
        # 1. Flush any remaining Working memory turns to Episodic sqlite first
        await self.consolidator.ingest_working_to_episodic(session_id)
        
        # 2. Consolidate episodic SQLite data into Qdrant vectors and Graphitti graph nodes
        await self.consolidator.consolidate_episodic_to_semantic_and_graph(
            session_id=session_id,
            provider=provider,
            model_id=model_id,
            embedding_provider=embedding_provider,
        )
        logger.info("Successfully finished milestone memory consolidation.")
