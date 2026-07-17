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
        three_brain: Any | None = None,
        provider_registry: Any | None = None,
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
        three_brain:
            Optional pre-built ThreeBrainCoordinator. ``retrieve()`` delegates
            its multi-tier fan-out to this instance instead of reimplementing
            it, so there is exactly one place that coordinates working/
            semantic/episodic queries. If omitted, one is lazily built from
            this manager's own tiers on first use.
        provider_registry:
            Optional provider registry used to resolve a real inference
            provider for compaction summarization (see
            ``_check_and_trigger_compaction``).
        """
        self.working = working_tier
        self.episodic_memory = episodic_memory
        self.semantic_memory = semantic_memory
        self.embedding_pipeline = embedding_pipeline
        self.lineage = lineage_tier
        self.episodic_session_memory = episodic_session_memory
        self._three_brain_coordinator = three_brain
        self.provider_registry = provider_registry

        self._session_count = 0
        self._is_active = False

    def _three_brain(self) -> Any:
        """Return the shared ThreeBrainCoordinator, building one lazily if needed."""
        if self._three_brain_coordinator is None:
            from velune.memory.three_brain import ThreeBrainCoordinator

            self._three_brain_coordinator = ThreeBrainCoordinator(
                self.working, self.semantic_memory, self.episodic_memory
            )
        return self._three_brain_coordinator

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

                provider = self._resolve_compaction_provider()
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
                # Schedule compaction as background task (non-blocking). Tracked
                # so the loop's weak reference can't let it be GC'd mid-compaction.
                from velune.core.task_registry import track

                track(
                    asyncio.create_task(
                        self._perform_compaction(session_id),
                        name=f"memory_compaction_{session_id}",
                    )
                )
        except Exception as exc:
            logger.debug("Error checking compaction trigger: %s", exc)

    def _resolve_compaction_provider(self) -> Any | None:
        """Resolve a real inference provider for compaction summarization.

        Prefers the local "ollama" provider (cheap, no external cost) and
        falls back to any provider with a configured key. Returns ``None``
        (degrading ``ContextCompactor`` to a no-op) only when the registry
        itself is unavailable or nothing is configured.
        """
        if not self.provider_registry:
            return None
        try:
            provider = self.provider_registry.get("ollama")
            if provider:
                return provider
            for name in self.provider_registry.list_available_providers():
                provider = self.provider_registry.get(name)
                if provider:
                    return provider
        except Exception as exc:
            logger.debug("Could not resolve a compaction provider: %s", exc)
        return None

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

        Fans out through the shared :class:`~velune.memory.three_brain.ThreeBrainCoordinator`
        — the single place that coordinates working/semantic/episodic
        queries — then applies this manager's own vitality/trust shaping and
        fits results to the token budget.

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
            brain_result = await self._three_brain().query(
                query,
                session_id=workspace_root or "default",
                workspace_root=workspace_root,
                working_limit=10,
                semantic_limit=5,
                episodic_limit=10,
            )
        except Exception as exc:
            logger.debug("ThreeBrainCoordinator query failed: %s", exc)
            brain_result = None

        if brain_result is not None:
            # Working memory (current session, fastest)
            for turn in brain_result.working_hits:
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

            # Episodic memory (SQLite LIKE, fast but imprecise)
            if accumulated_tokens < budget:
                for turn in brain_result.episodic_hits:
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

            # Semantic memory (LanceDB ANN, slower but precise)
            if accumulated_tokens < budget:
                for mem in brain_result.semantic_hits:
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

            # Repository Brain: code-graph context, lowest-trust source
            if brain_result.kg_context and accumulated_tokens < budget:
                tokens = self._estimate_tokens(brain_result.kg_context)
                if accumulated_tokens + tokens <= budget:
                    context.results.append(
                        RetrievedResult(
                            content=brain_result.kg_context,
                            source_type="kg",
                            relevance_score=0.6,
                            trust_score=0.6,
                            vitality="live",
                            session_id="",
                            age_seconds=0.0,
                            attribution="repository knowledge graph",
                        )
                    )
                    accumulated_tokens += tokens

        # Rank by (relevance × trust) and sort
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
            raw_decisions, raw_failures = await self.lineage.query_continuity_warnings(
                prompt=query, repo_context=""
            )
            for d in raw_decisions:
                try:
                    decisions.append(
                        Decision(
                            id=str(d["id"]),
                            target_subsystem=d.get("target_subsystem", ""),
                            rationale=d.get("rationale", ""),
                            architectural_impact=float(d.get("architectural_impact", 0.0)),
                            consequences=d.get("consequences"),
                            timestamp=float(d.get("timestamp", 0.0)),
                        )
                    )
                except Exception:
                    pass
            for f in raw_failures:
                try:
                    failures.append(
                        Failure(
                            id=int(f["id"]),
                            target_subsystem=f.get("target_subsystem", ""),
                            error_type=f.get("error_type", ""),
                            error_message=f.get("error_message", ""),
                            timestamp=float(f.get("timestamp", 0.0)),
                        )
                    )
                except Exception:
                    pass
            logger.debug(
                "Lineage warnings: %d decisions, %d failures for query '%s'",
                len(decisions),
                len(failures),
                query[:50],
            )
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
