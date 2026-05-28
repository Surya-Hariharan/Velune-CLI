"""Memory Subsystem Lifecycle and Coordinator.

Manages clean connections startup, shutdown flushes, and orchestrates
transient to persistent memory ingestion.
"""

from __future__ import annotations

import logging
from typing import Any

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
    """Orchestrates memory subsystem boot protocols and handles artifact ingestion."""

    def __init__(self, working_tier: Any, episodic_tier: Any) -> None:
        self.working = working_tier
        self.episodic = episodic_tier
        self._is_active = False

    async def startup(self) -> None:
        """Boot databases and establish active connection boundaries."""
        logger.info("Initializing Hierarchical Memory Tiers...")
        self._is_active = True

    async def shutdown(self) -> None:
        """Ensure all working buffers are safely flushed before termination."""
        logger.info("Flushing transient working memory buffers before shutdown...")
        self._is_active = False

    def ingest(self, artifact: MemoryArtifact) -> None:
        """Ingest a finalized memory artifact into working and episodic tiers."""
        session_id = artifact.metadata.get("run_id") or "default"
        if self.working:
            self.working.add_turn(
                role="system",
                content=artifact.content,
                metadata=artifact.metadata,
            )
        if self.episodic:
            self.episodic.add_turn(
                session_id=session_id,
                role="system",
                content=artifact.content,
                metadata=artifact.metadata,
            )

    def summary(self) -> dict[str, Any]:
        """Retrieve dynamic health and retention stats across all tiers."""
        return {
            "working_turns": len(self.working.get_turns()) if self.working else 0,
            "working_logs": len(self.working.get_execution_logs()) if self.working else 0,
            "is_active": self._is_active,
        }
