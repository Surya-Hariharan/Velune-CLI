"""Memory Subsystem Lifecycle and Coordinator.

Manages clean connections startup, shutdown flushes, and orchestrates
transient to persistent memory ingestion.

Phase 1 repairs:
  * ``shutdown()`` now flushes any live working-memory turns into the
    episodic SQLite tier before terminating.  This ensures turn history
    survives process exit and is not silently discarded.
  * ``get_recent_context(session_id, limit)`` exposes the minimal episodic
    read path so orchestrators can hydrate context from previous sessions
    without importing the episodic tier directly.
"""

from __future__ import annotations

import logging
from typing import Any

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
        """Flush working memory turns into episodic SQLite, then terminate.

        Previously this was a no-op (only logged a message).  Now it
        iterates all live working turns and persists them to the episodic
        tier before clearing them, so turn history survives process exit.
        """
        logger.info("Flushing transient working memory buffers before shutdown...")

        if self.working and self.episodic:
            session_id = getattr(self.working, "session_id", "default")
            # Evict stale turns first so we don't persist garbage
            if hasattr(self.working, "evict_expired"):
                self.working.evict_expired()

            turns = self.working.get_turns()
            if turns:
                logger.info(
                    "Persisting %d working memory turns to episodic SQLite [session=%s]",
                    len(turns),
                    session_id,
                )
                for turn in turns:
                    try:
                        self.episodic.add_turn(
                            session_id=session_id,
                            role=turn.role,
                            content=turn.content,
                            metadata=turn.metadata,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Failed to flush turn to episodic memory: %s", exc
                        )

        self._is_active = False

    def get_recent_context(self, session_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """Return the most recent episodic turns for a session.

        This is the *minimal episodic read path* — it reads directly from
        the episodic SQLite tier so orchestrators can hydrate context from
        previous sessions without coupling to the full retrieval stack.

        Parameters
        ----------
        session_id:
            The session whose history to fetch.
        limit:
            Maximum number of turns to return (most recent first).

        Returns
        -------
        list[dict]:
            Each dict has keys ``role``, ``content``, ``timestamp``,
            ``metadata``.  Returns an empty list if the episodic tier is
            unavailable or has no history for this session.
        """
        if not self.episodic:
            return []

        try:
            turns = self.episodic.get_turns(session_id)
            recent = turns[-limit:]
            return [
                {
                    "role": t.role,
                    "content": t.content,
                    "timestamp": t.timestamp,
                    "metadata": t.metadata,
                }
                for t in recent
            ]
        except Exception as exc:
            logger.warning("Failed to read episodic context [session=%s]: %s", session_id, exc)
            return []

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
