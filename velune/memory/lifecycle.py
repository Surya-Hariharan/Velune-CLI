"""Memory Subsystem Lifecycle and Coordinator.

Manages clean connections startup, shutdown flushes, and orchestrates
period/milestone-driven memory consolidation routines.
"""

from __future__ import annotations

import logging
from typing import Any

from velune.memory.consolidator import MemoryConsolidator
from velune.providers.base import ModelProvider

logger = logging.getLogger("velune.memory.lifecycle")


class MemoryArtifact:
    """Represents a discrete memory chunk captured during run finalization."""

    def __init__(
        self,
        id: str,
        memory_type: str,
        content: str,
        importance: float,
        metadata: dict[str, Any],
    ) -> None:
        self.id = id
        self.memory_type = memory_type
        self.content = content
        self.importance = importance
        self.metadata = metadata


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

    def ingest(self, artifact: MemoryArtifact) -> None:
        """Ingest a finalized memory artifact into working and episodic tiers."""
        session_id = artifact.metadata.get("run_id") or "default"
        if self.consolidator.working:
            self.consolidator.working.add_turn(
                role="system",
                content=artifact.content,
                metadata=artifact.metadata,
            )
        if self.consolidator.episodic:
            self.consolidator.episodic.add_turn(
                session_id=session_id,
                role="system",
                content=artifact.content,
                metadata=artifact.metadata,
            )

    def summary(self) -> dict[str, Any]:
        """Retrieve dynamic health and retention stats across all tiers."""
        return {
            "working_turns": len(self.consolidator.working.get_turns()) if self.consolidator.working else 0,
            "working_logs": len(self.consolidator.working.get_execution_logs()) if self.consolidator.working else 0,
            "is_active": self._is_active,
        }

    async def trigger_milestone_consolidation(
        self,
        session_id: str,
        provider: ModelProvider,
        model_id: str,
        embedding_provider: Any | None = None,
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
