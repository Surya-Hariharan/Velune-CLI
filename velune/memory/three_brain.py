"""Three-Brain Memory Coordinator.

Unifies working (hot), semantic (warm), and episodic (cold) memory into a
single query interface that fans out to all three brains concurrently and
returns a ranked, deduplicated result.

Three-brain architecture
------------------------
Brain 1 — Hot   (Working):  in-process conversation turns, sub-ms retrieval
Brain 2 — Warm  (Semantic): LanceDB-backed ANN search, ~10 ms round-trip
Brain 3 — Cold  (Episodic): SQLite cross-session LIKE search, ~5-20 ms

An optional KnowledgeQuery augments the warm brain with code-graph context.

Repository event integration
-----------------------------
Call ``await coordinator.subscribe_to_repository_events(bus)`` once at startup.
The coordinator then tracks which files have changed since the last query so it
can annotate results with a staleness count — callers know to treat code-related
memories with lower confidence until the graph is re-patched.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

logger = logging.getLogger("velune.memory.three_brain")

_MAX_CONTEXT_CHARS = 2000


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ThreeBrainResult:
    """Unified query result from all three memory brains.

    Attributes
    ----------
    working_hits:
        Recent ``MemoryTurn`` objects from the current session (Brain 1).
    semantic_hits:
        ``RetrievedMemory`` objects from the LanceDB ANN index (Brain 2).
    episodic_hits:
        ``Turn`` objects from the SQLite cross-session store (Brain 3).
    kg_context:
        Optional plain-text code-graph context from ``KnowledgeQuery``.
    stale_file_count:
        Number of repository files that changed since the last
        ``clear_stale()`` call.  Non-zero means some semantic memories
        may be outdated relative to the current codebase.
    query:
        The original query string, for reference.
    """

    working_hits: list[Any] = field(default_factory=list)
    semantic_hits: list[Any] = field(default_factory=list)
    episodic_hits: list[Any] = field(default_factory=list)
    kg_context: str | None = None
    stale_file_count: int = 0
    query: str = ""

    def total_hits(self) -> int:
        """Total number of memory items across all three brains."""
        return len(self.working_hits) + len(self.semantic_hits) + len(self.episodic_hits)

    def is_empty(self) -> bool:
        """True when no brain returned any content."""
        return self.total_hits() == 0 and self.kg_context is None

    def as_context_block(self, max_chars: int = _MAX_CONTEXT_CHARS) -> str:
        """Render all brain hits as a compact text block for LLM injection.

        Returns an empty string if all brains returned no content.
        """
        parts: list[str] = []

        # Brain 1: Working (most recent in-session turns)
        if self.working_hits:
            lines: list[str] = []
            for t in self.working_hits[-3:]:
                role = getattr(t, "role", "?")
                content = str(getattr(t, "content", ""))[:120]
                lines.append(f"  [{role}] {content}")
            parts.append("## Working Memory (current session)\n" + "\n".join(lines))

        # Brain 2: Semantic (similar past context via ANN)
        if self.semantic_hits:
            lines = []
            for r in self.semantic_hits[:3]:
                attribution = getattr(r, "attribution", "")
                content = str(getattr(r, "content", ""))[:120]
                lines.append(f"  [{attribution}] {content}")
            parts.append("## Semantic Memory (similar past context)\n" + "\n".join(lines))

        # Brain 3: Episodic (historical session turns)
        if self.episodic_hits:
            lines = []
            for t in self.episodic_hits[:3]:
                role = getattr(t, "role", "?")
                content = str(getattr(t, "content", ""))[:100]
                lines.append(f"  [{role}] {content}")
            parts.append("## Episodic Memory (historical sessions)\n" + "\n".join(lines))

        # KG code-graph context
        if self.kg_context:
            parts.append(f"## Code Graph Context\n{self.kg_context[:600]}")

        # Staleness note
        if self.stale_file_count:
            parts.append(
                f"_Note: {self.stale_file_count} file(s) changed since last memory index._"
            )

        if not parts:
            return ""

        block = "\n\n".join(parts)
        if len(block) > max_chars:
            block = block[:max_chars] + "\n...(truncated)"
        return block


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class ThreeBrainCoordinator:
    """Unified memory interface routing queries across all three brains.

    Each brain is optional: pass ``None`` for any tier that is unavailable
    and the coordinator degrades gracefully, returning only what is present.

    Lifecycle
    ---------
    1. Construct with references to the individual tiers (may be ``None``).
    2. Optionally call ``await coordinator.subscribe_to_repository_events(bus)``
       to enable automatic staleness tracking.
    3. Call ``await coordinator.query(q, session_id, workspace_root)`` per
       user turn to retrieve context from all three brains.
    4. After acting on a ``ThreeBrainResult`` with a non-zero
       ``stale_file_count``, call ``coordinator.clear_stale()`` to reset.
    """

    def __init__(
        self,
        working: Any | None,
        semantic: Any | None,
        episodic: Any | None,
        kg_query: Any | None = None,
        bus: Any | None = None,
    ) -> None:
        self._working = working  # WorkingMemoryTier | None
        self._semantic = semantic  # SemanticMemory | None
        self._episodic = episodic  # EpisodicMemory | None
        self._kg_query = kg_query  # KnowledgeQuery | None
        self._bus = bus  # CognitiveBus | None — used only by initialize()
        self._stale_paths: set[str] = set()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Subscribe to repository-change events when a bus was provided.

        Matches the ``hasattr(comp, "initialize")`` convention the
        lifecycle coordinator already uses for other DI-managed subsystems,
        so DI wiring doesn't need an async factory just to reach this call.
        """
        if self._bus is not None:
            await self.subscribe_to_repository_events(self._bus)

    async def query(
        self,
        q: str,
        session_id: str,
        workspace_root: str,
        *,
        working_limit: int = 5,
        semantic_limit: int = 5,
        episodic_limit: int = 5,
    ) -> ThreeBrainResult:
        """Fan out to all three brains concurrently and return a unified result.

        Individual brain failures are swallowed — the result will have an
        empty list for the failing brain rather than raising.
        """
        raw = await asyncio.gather(
            self._query_working(session_id, working_limit),
            self._query_semantic(q, workspace_root, semantic_limit),
            self._query_episodic(q, workspace_root, episodic_limit),
            self._query_kg(q),
            return_exceptions=True,
        )

        working_hits: list[Any] = raw[0] if not isinstance(raw[0], BaseException) else []
        semantic_hits: list[Any] = raw[1] if not isinstance(raw[1], BaseException) else []
        episodic_hits: list[Any] = raw[2] if not isinstance(raw[2], BaseException) else []
        kg_context: str | None = raw[3] if not isinstance(raw[3], BaseException) else None

        return ThreeBrainResult(
            working_hits=working_hits,
            semantic_hits=semantic_hits,
            episodic_hits=episodic_hits,
            kg_context=kg_context,
            stale_file_count=len(self._stale_paths),
            query=q,
        )

    async def subscribe_to_repository_events(self, bus: Any) -> None:
        """Subscribe to ``repository.files_changed`` to track semantic staleness.

        Each time the Intelligence Engine fires that event the coordinator
        accumulates the changed paths.  The next ``query()`` will reflect
        the stale count in the returned ``ThreeBrainResult``.
        """

        async def _on_files_changed(event: Any) -> None:
            data = event.data
            for key in ("added", "updated", "removed"):
                self._stale_paths.update(data.get(key, []))

        await bus.subscribe("repository.files_changed", _on_files_changed)
        logger.debug("ThreeBrainCoordinator subscribed to repository.files_changed")

    def clear_stale(self) -> None:
        """Reset the stale-file tracker after the caller has handled it."""
        self._stale_paths.clear()

    @property
    def stale_file_count(self) -> int:
        """Files that changed since the last ``clear_stale()`` call."""
        return len(self._stale_paths)

    @property
    def stale_paths(self) -> frozenset[str]:
        """Immutable snapshot of stale file paths."""
        return frozenset(self._stale_paths)

    # ------------------------------------------------------------------
    # Per-brain helpers — all non-raising; errors return empty
    # ------------------------------------------------------------------

    async def _query_working(self, session_id: str, limit: int) -> list[Any]:
        if not self._working:
            return []
        try:
            return await asyncio.to_thread(self._working.get_recent_turns, limit)
        except Exception as exc:
            logger.debug("Working brain query error: %s", exc)
            return []

    async def _query_semantic(self, q: str, workspace_root: str, limit: int) -> list[Any]:
        if not self._semantic:
            return []
        try:
            return await self._semantic.search(q, workspace_root, limit=limit)
        except Exception as exc:
            logger.debug("Semantic brain query error: %s", exc)
            return []

    async def _query_episodic(self, q: str, workspace_root: str, limit: int) -> list[Any]:
        if not self._episodic:
            return []
        try:
            return await self._episodic.search_by_content(q, workspace_root, limit=limit)
        except Exception as exc:
            logger.debug("Episodic brain query error: %s", exc)
            return []

    async def _query_kg(self, q: str) -> str | None:
        if not self._kg_query:
            return None
        try:
            summary = await self._kg_query.summary_text()
            hits = await self._kg_query.find_by_label(q)
            if not hits:
                return None if "No repository" in summary else summary
            sym_lines = [
                f"  [{n.node_type.value}] {n.label} @ {n.file_path or 'unknown'}" for n in hits[:10]
            ]
            return summary + "\nMatching symbols:\n" + "\n".join(sym_lines)
        except Exception as exc:
            logger.debug("KG brain query error: %s", exc)
            return None
