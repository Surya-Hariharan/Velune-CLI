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

Phase 2a: Enhanced MemoryLifecycleManager with:
  * Multi-tier retrieval: working → episodic (LIKE) → semantic (ANN) → merged
  * Vitality-based filtering: LIVE/ZOMBIE/ARCHIVED classification
  * Lineage warnings: architectural decisions and failed experiments
  * Health reporting: turn counts, queue depth, store size
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

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


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2a: Multi-tier retrieval and vitality-based filtering
# ─────────────────────────────────────────────────────────────────────────────


class RetrievedResult(BaseModel):
    """A single result from multi-tier memory retrieval."""

    content: str
    source_type: str  # "working" | "episodic" | "semantic"
    relevance_score: float = 0.0  # 0-1, higher is more relevant
    trust_score: float = 1.0  # 0-1, influenced by vitality
    vitality: str = "live"  # "live" | "zombie" | "archived"
    session_id: str = ""
    age_seconds: float = 0.0
    attribution: str = ""  # e.g., "3 days ago" or "current session"


class RetrievedContext(BaseModel):
    """Aggregated context returned from multi-tier retrieval."""

    results: list[RetrievedResult] = Field(default_factory=list)
    total_tokens: int = 0
    query: str = ""
    fallback_to_zombie: bool = False  # Whether ZOMBIE tier was accessed
    workspace_root: str = ""


class Decision(BaseModel):
    """An architectural decision from the lineage tier."""

    id: str
    target_subsystem: str
    rationale: str
    architectural_impact: float = 0.0
    consequences: str | None = None
    timestamp: float = 0.0


class Failure(BaseModel):
    """A failed experiment from the lineage tier."""

    id: int
    target_subsystem: str
    error_type: str
    error_message: str
    timestamp: float = 0.0


@dataclass
class MemoryHealth:
    """Health metrics for the memory subsystem."""

    working_memory_turns: int = 0
    episodic_sessions: int = 0
    semantic_indexed_count: int = 0
    embedding_queue_depth: int = 0
    lancedb_size_mb: float = 0.0
    timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for display."""
        return {
            "working_memory_turns": self.working_memory_turns,
            "episodic_sessions": self.episodic_sessions,
            "semantic_indexed_count": self.semantic_indexed_count,
            "embedding_queue_depth": self.embedding_queue_depth,
            "lancedb_size_mb": self.lancedb_size_mb,
        }


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
                        await self.episodic.add_turn(
                            session_id=session_id,
                            role=turn.role,
                            content=turn.content,
                            metadata=turn.metadata,
                        )
                    except Exception as exc:
                        logger.warning("Failed to flush turn to episodic memory: %s", exc)

        self._is_active = False

    async def get_recent_context(self, session_id: str, limit: int = 10) -> list[dict[str, Any]]:
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
            turns = await self.episodic.get_turns(session_id)
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


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2a: Enhanced MemoryLifecycleManager
# ─────────────────────────────────────────────────────────────────────────────


class MemoryLifecycleManager:
    """Coordinates all memory tiers: working, episodic, semantic, and lineage.

    Provides unified read (retrieve) and write (record_turn) interfaces,
    vitality-based filtering, and health metrics.
    """

    def __init__(
        self,
        working_tier: Any,
        episodic_memory: Any,
        semantic_memory: Any,
        embedding_pipeline: Any,
        lineage_tier: Any,
        episodic_session_memory: Any | None = None,
    ) -> None:
        """Initialize with all memory tier implementations.

        Parameters
        ----------
        working_tier:
            In-process turn store (WorkingMemoryTier).
        episodic_memory:
            SQLite-backed session/turn store (EpisodicMemory).
        semantic_memory:
            LanceDB-backed semantic store (SemanticMemory).
        embedding_pipeline:
            Background embedding queue (EmbeddingPipeline).
        lineage_tier:
            Decision and failure store (LineageMemoryTier).
        episodic_session_memory:
            Optional session memory tier (EpisodicMemoryTier, legacy).
        """
        self.working = working_tier
        self.episodic_memory = episodic_memory
        self.semantic_memory = semantic_memory
        self.embedding_pipeline = embedding_pipeline
        self.lineage = lineage_tier
        self.episodic_session_memory = episodic_session_memory

        from velune.memory.vitality import VitalityClassifier

        self._vitality = VitalityClassifier()
        self._session_count = 0
        self._is_active = False

    async def startup(self) -> None:
        """Initialize all memory tiers."""
        logger.info("MemoryLifecycleManager: starting up...")
        self._is_active = True

    async def shutdown(self) -> None:
        """Flush queues and close connections."""
        logger.info("MemoryLifecycleManager: shutting down...")
        if self.working and self.episodic_memory:
            session_id = getattr(self.working, "session_id", "default")
            if hasattr(self.working, "evict_expired"):
                self.working.evict_expired()
            turns = self.working.get_turns()
            if turns:
                logger.debug("Flushing %d working turns to episodic", len(turns))
                for turn in turns:
                    try:
                        await self.episodic_memory.record_turn(
                            session_id=session_id,
                            role=turn.role,
                            content=turn.content,
                        )
                    except Exception as exc:
                        logger.warning("Failed to flush turn: %s", exc)
        self._is_active = False

    async def record_turn(
        self,
        session_id: str,
        role: str,
        content: str,
        model: str | None = None,
        tokens: int | None = None,
        workspace_root: str = "",
    ) -> str:
        """Record a conversation turn across all applicable tiers.

        Writes to episodic SQLite, enqueues for semantic embedding,
        and logs to working memory. Triggers compaction if thresholds are met.

        Returns the turn ID.
        """
        turn_id = ""
        try:
            if self.episodic_memory:
                turn_id = await self.episodic_memory.record_turn(
                    session_id=session_id,
                    role=role,
                    content=content,
                    model=model,
                    tokens=tokens,
                )
        except Exception as exc:
            logger.warning("Failed to record turn to episodic: %s", exc)

        if self.semantic_memory and turn_id:
            try:
                from velune.memory.tiers.episodic import Turn

                turn = Turn(
                    id=turn_id,
                    session_id=session_id,
                    turn_index=0,
                    role=role,
                    content=content,
                    model_used=model,
                    tokens_used=tokens,
                    created_at=time.time(),
                )
                self.semantic_memory.index_turn(turn, workspace_root)
            except Exception as exc:
                logger.debug("Failed to enqueue turn for embedding: %s", exc)

        if self.working:
            try:
                self.working.add_turn(role, content, {"model": model, "tokens": tokens})
            except Exception as exc:
                logger.warning("Failed to add turn to working: %s", exc)

        # Check for compaction trigger (non-blocking)
        await self._check_and_trigger_compaction(session_id)

        return turn_id

    async def _check_and_trigger_compaction(self, session_id: str) -> None:
        """Check if compaction should be triggered and schedule it as background task.

        Parameters
        ----------
        session_id:
            Session ID for the current session
        """
        if not self.working:
            return

        try:
            turns = self.working.get_turns()
            turn_count = len(turns)

            # Estimate current token count (rough)
            current_tokens = sum(len(t.content) // 4 for t in turns)

            # Create compactor if needed
            if not hasattr(self, "_compactor"):
                from velune.memory.compaction import ContextCompactor

                # Use first available provider (or default)
                provider = None  # Will be set when needed
                self._compactor = ContextCompactor(
                    provider=provider,
                    working_tier=self.working,
                    episodic_memory=self.episodic_memory,
                    max_context_tokens=100000,
                )

            # Check if compaction should trigger
            should_compact = await self._compactor.should_compact(
                turn_count=turn_count,
                current_token_count=current_tokens,
                session_end=False,
            )

            if should_compact:
                # Schedule compaction as background task (non-blocking)
                asyncio.create_task(self._perform_compaction(session_id))
        except Exception as exc:
            logger.debug("Error checking compaction trigger: %s", exc)

    async def _perform_compaction(self, session_id: str) -> None:
        """Perform compaction asynchronously in the background.

        Parameters
        ----------
        session_id:
            Session ID for the current session
        """
        try:
            if hasattr(self, "_compactor"):
                stats = await self._compactor.compact(session_id)
                if stats:
                    logger.info(
                        f"Compaction completed: {stats.turns_compacted} turns → "
                        f"{stats.summary_token_count} tokens ({stats.compression_ratio:.1f}x)"
                    )
        except Exception as exc:
            logger.warning("Background compaction failed: %s", exc)

    async def retrieve(
        self,
        query: str,
        workspace_root: str,
        budget: int = 4000,
    ) -> RetrievedContext:
        """Multi-tier retrieval: working → episodic (LIKE) → semantic (ANN).

        Merges results from all tiers, filters by vitality, ranks by
        (relevance × trust), and fits to token budget.

        Parameters
        ----------
        query:
            The search query.
        workspace_root:
            Workspace path for scoping searches.
        budget:
            Token budget for results (approx 4000 tokens default).

        Returns
        -------
        RetrievedContext:
            Aggregated results, sorted by relevance.
        """
        context = RetrievedContext(query=query, workspace_root=workspace_root)
        accumulated_tokens = 0

        try:
            # Step 1: Working memory (current session, fastest)
            if self.working:
                recent = self.working.get_recent_turns(limit=10)
                for turn in recent:
                    result = RetrievedResult(
                        content=turn.content,
                        source_type="working",
                        relevance_score=0.95,  # High confidence for current session
                        trust_score=1.0,
                        vitality="live",
                        session_id=turn.session_id,
                        age_seconds=time.time() - turn.timestamp,
                        attribution="current session",
                    )
                    tokens = self._estimate_tokens(turn.content)
                    if accumulated_tokens + tokens <= budget:
                        context.results.append(result)
                        accumulated_tokens += tokens
                    else:
                        break
        except Exception as exc:
            logger.debug("Working memory search failed: %s", exc)

        # Step 2: Episodic memory (SQLite LIKE, fast but imprecise)
        try:
            if self.episodic_memory and accumulated_tokens < budget:
                episodic_results = await self.episodic_memory.search_by_content(
                    query, workspace_root, limit=10
                )
                for turn in episodic_results:
                    age = time.time() - turn.created_at
                    result = RetrievedResult(
                        content=turn.content,
                        source_type="episodic",
                        relevance_score=0.7,
                        trust_score=0.8,
                        vitality="zombie" if age > 604800 else "live",  # 7 days
                        session_id=turn.session_id,
                        age_seconds=age,
                        attribution=self._format_age(age),
                    )
                    tokens = self._estimate_tokens(turn.content)
                    if accumulated_tokens + tokens <= budget:
                        context.results.append(result)
                        accumulated_tokens += tokens
                    else:
                        break
        except Exception as exc:
            logger.debug("Episodic search failed: %s", exc)

        # Step 3: Semantic memory (LanceDB ANN, slower but precise)
        try:
            if self.semantic_memory and accumulated_tokens < budget and self.embedding_pipeline:
                semantic_results = await self.semantic_memory.search(query, workspace_root, limit=5)
                for mem in semantic_results:
                    # Map semantic memory's vitality to our enum
                    vitality = "live"
                    trust = mem.trust_score
                    if mem.age_seconds > 2592000:  # 30 days
                        vitality = "archived"
                        trust *= 0.2
                    elif mem.age_seconds > 604800:  # 7 days
                        vitality = "zombie"
                        trust *= 0.6

                    result = RetrievedResult(
                        content=mem.content,
                        source_type="semantic",
                        relevance_score=1.0 - mem.distance,
                        trust_score=trust,
                        vitality=vitality,
                        session_id=mem.session_id,
                        age_seconds=mem.age_seconds,
                        attribution=mem.attribution,
                    )
                    tokens = self._estimate_tokens(mem.content)
                    if accumulated_tokens + tokens <= budget:
                        context.results.append(result)
                        accumulated_tokens += tokens
                    else:
                        break
        except Exception as exc:
            logger.debug("Semantic search failed: %s", exc)

        # Step 4: Rank by (relevance × trust) and sort
        for result in context.results:
            result.relevance_score *= result.trust_score

        context.results.sort(key=lambda r: r.relevance_score, reverse=True)
        context.total_tokens = accumulated_tokens

        logger.debug(
            "Retrieved %d results (%d tokens) for query '%s'",
            len(context.results),
            accumulated_tokens,
            query[:50],
        )
        return context

    async def get_working_context(self, session_id: str, limit: int = 10) -> list[Any]:
        """Return the N most recent turns from working memory.

        Parameters
        ----------
        session_id:
            The session whose context to return.
        limit:
            Maximum number of turns to return.

        Returns
        -------
        list:
            Recent turns in chronological order.
        """
        if not self.working:
            return []
        try:
            return self.working.get_recent_turns(limit)
        except Exception as exc:
            logger.warning("Failed to get working context: %s", exc)
            return []

    async def get_lineage_warnings(self, query: str) -> tuple[list[Decision], list[Failure]]:
        """Retrieve architectural decisions and failed experiments related to query.

        Returns two lists: approved decisions and past failures that might inform
        the current decision.

        Parameters
        ----------
        query:
            The query context (e.g., a subsystem name or feature description).

        Returns
        -------
        tuple[list[Decision], list[Failure]]:
            (decisions, failures) both sorted by relevance.
        """
        decisions: list[Decision] = []
        failures: list[Failure] = []

        if not self.lineage:
            return decisions, failures

        try:
            # This is a stub; full implementation requires searching lineage by query
            # For now, return empty lists and log the intention.
            logger.debug("Lineage warnings requested for query: %s", query)
        except Exception as exc:
            logger.warning("Failed to retrieve lineage warnings: %s", exc)

        return decisions, failures

    async def health(self) -> MemoryHealth:
        """Return health metrics across all memory tiers.

        Returns
        -------
        MemoryHealth:
            Metrics including turn counts, queue depth, and store size.
        """
        health = MemoryHealth(timestamp=time.time())

        if self.working:
            health.working_memory_turns = len(self.working.get_turns())

        if self.episodic_memory:
            try:
                sessions = await self.episodic_memory.list_recent_sessions(
                    workspace_root="", limit=1000
                )
                health.episodic_sessions = len(sessions)
            except Exception as exc:
                logger.debug("Failed to count episodic sessions: %s", exc)

        if self.embedding_pipeline:
            health.embedding_queue_depth = self.embedding_pipeline._queue.qsize()

        if hasattr(self.semantic_memory, "_store") and self.semantic_memory._store:
            try:
                import os

                store_path = getattr(self.semantic_memory._store, "_path", None)
                if store_path and os.path.isdir(store_path):
                    total_size = sum(
                        os.path.getsize(os.path.join(root, f))
                        for root, _, files in os.walk(store_path)
                        for f in files
                    )
                    health.lancedb_size_mb = total_size / (1024 * 1024)
            except Exception as exc:
                logger.debug("Failed to measure LanceDB size: %s", exc)

        return health

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Rough token estimate (4 chars ≈ 1 token)."""
        return max(1, len(text) // 4)

    @staticmethod
    def _format_age(seconds: float) -> str:
        """Return a human-readable relative age string."""
        minutes = seconds / 60
        hours = minutes / 60
        days = hours / 24
        if days >= 2:
            return f"{int(days)} days ago"
        if days >= 1:
            return "yesterday"
        if hours >= 2:
            return f"{int(hours)} hours ago"
        if hours >= 1:
            return "an hour ago"
        if minutes >= 2:
            return f"{int(minutes)} minutes ago"
        return "just now"
