"""Code vector store connection (Qdrant) for repository/symbol retrieval.

This used to be "Semantic Memory Tier 3": a Qdrant-backed class
(``SemanticMemoryTier``) with its own ``upsert_points``/``search_similarity``/
``delete_points``/``delete_by_payload`` API, describing itself as the
conversational semantic-memory tier. Phase 2a added :class:`SemanticMemory`
below — a LanceDB-backed tier with an async embedding pipeline and vitality-
aware retrieval — as conversational memory's *real* backend, and every
caller (``MemoryLifecycleManager``, ``ThreeBrainCoordinator``, turn
recording) moved onto it. The old tier's own read/write API was never called
again by anything outside this file, but the class and its "semantic memory"
framing stayed, making it look like two competing conversational-memory
backends existed side by side.

They didn't: the only thing anyone still used from the Qdrant class was its
lazily-initialized ``.client`` property, handed to
:class:`~velune.retrieval.vector.VectorRetriever` for code/symbol search — a
different job (repository content, not conversation turns) that happens to
also want a vector index. :class:`CodeVectorConnection` is that class,
renamed and stripped down to just what it actually does; the dead tier-API
methods are gone. There is now exactly one conversational vector memory
backend (:class:`SemanticMemory`, LanceDB) and one code-search vector
connection (this one, Qdrant) — not two competing memory tiers.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

# NOTE: qdrant_client (and its compiled local-mode backend) is imported lazily
# inside _ensure_client(). Importing it at module load — and constructing the
# client — was a multi-second cost on the startup path, especially with the
# store on a cloud-synced drive. The connection is needed only once a
# retrieval operation actually runs, so we defer both the import and the
# connection.

logger = logging.getLogger("velune.memory.tiers.semantic")


class CodeVectorConnection:
    """Lazy, degradable Qdrant connection for code/repository vector search.

    Not a memory tier in its own right — it exists to hand
    :class:`~velune.retrieval.vector.VectorRetriever` a shared Qdrant client
    (see ``.client``) without paying Qdrant's startup cost when retrieval
    never runs. Set ``VELUNE_SKIP_QDRANT=1`` to force degraded mode (client
    stays ``None``; callers already treat that as "vector search
    unavailable, fall back to lexical").
    """

    def __init__(
        self,
        location: str = ":memory:",
        url: str | None = None,
        api_key: str | None = None,
        path: str | None = None,
    ) -> None:
        self._location = location
        self._url = url
        self._api_key = api_key
        self._path = path
        self._client: Any = None
        self._degraded = os.environ.get("VELUNE_SKIP_QDRANT", "").lower() in ("1", "true", "yes")
        if self._degraded:
            logger.warning(
                "VELUNE_SKIP_QDRANT set — code vector search running in degraded (no-op) mode."
            )

    def _ensure_client(self) -> Any:
        """Create (once) and return the Qdrant client, or None in degraded mode."""
        if self._degraded:
            return None
        if self._client is not None:
            return self._client
        try:
            from qdrant_client import QdrantClient

            if self._url:
                logger.debug("Initializing Qdrant remote client at %s", self._url)
                self._client = QdrantClient(url=self._url, api_key=self._api_key)
            elif self._path:
                logger.debug("Initializing Qdrant in-process local storage at %s", self._path)
                self._client = QdrantClient(path=self._path)
            else:
                logger.debug("Initializing Qdrant volatile in-memory client (:memory:)")
                self._client = QdrantClient(location=self._location)
        except Exception as exc:
            # Degrade gracefully rather than crashing the whole runtime.
            logger.error("Qdrant initialization failed; code vector search degraded: %s", exc)
            self._degraded = True
            return None
        return self._client

    @property
    def client(self) -> Any:
        """Backward-compatible accessor; triggers lazy initialization."""
        return self._ensure_client()


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2a: LanceDB-backed SemanticMemory
# ─────────────────────────────────────────────────────────────────────────────


class RetrievedMemory:
    """A semantically matched memory returned to the REPL's context assembly."""

    __slots__ = (
        "content",
        "source_type",
        "distance",
        "trust_score",
        "session_id",
        "age_seconds",
        "attribution",
    )

    def __init__(
        self,
        content: str,
        source_type: str,
        distance: float,
        trust_score: float,
        session_id: str,
        age_seconds: float,
        attribution: str,
    ) -> None:
        self.content = content
        self.source_type = source_type
        self.distance = distance
        self.trust_score = trust_score
        self.session_id = session_id
        self.age_seconds = age_seconds
        self.attribution = attribution


def _format_age(seconds: float) -> str:
    """Return a human-readable relative age string for *seconds* elapsed."""
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


class SemanticMemory:
    """Phase-2a semantic memory backed by LanceDB and an async embedding pipeline.

    ``index_turn()`` is intentionally non-blocking: it enqueues the turn and
    returns immediately.  The slow Ollama call and LanceDB write happen in a
    background worker task owned by :class:`~velune.memory.embedding_pipeline.EmbeddingPipeline`.

    Usage
    -----
    * Call ``await memory.search(query, workspace_root)`` from the REPL to retrieve
      semantically similar past interactions before calling the model.
    * Call ``memory.index_turn(turn, workspace_root)`` after each conversation turn
      (non-blocking).
    """

    def __init__(self, store: Any, pipeline: Any) -> None:
        self._store = store
        self._pipeline = pipeline

    # ── Search ─────────────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        workspace_root: str,
        limit: int = 5,
    ) -> list[RetrievedMemory]:
        """Embed *query* and return the *limit* most semantically similar memories."""
        if not self._pipeline or not self._store:
            return []
        try:
            embedding = await self._pipeline.embed_text(query)
        except Exception as exc:
            logger.debug("SemanticMemory.search — embedding failed: %s", exc)
            return []

        try:
            results = await self._store.search(
                embedding, limit=limit, workspace_root=workspace_root
            )
        except Exception as exc:
            logger.debug("SemanticMemory.search — LanceDB query failed: %s", exc)
            return []

        now = time.time()
        memories: list[RetrievedMemory] = []
        for r in results:
            age = max(0.0, now - r.created_at)
            memories.append(
                RetrievedMemory(
                    content=r.content,
                    source_type=r.source_type,
                    distance=r.distance,
                    trust_score=r.trust_score,
                    session_id=r.session_id,
                    age_seconds=age,
                    attribution=_format_age(age),
                )
            )
        return memories

    # ── Indexing ──────────────────────────────────────────────────────────────

    def index_turn(self, turn: Any, workspace_root: str = "") -> None:
        """Non-blocking: enqueue *turn* for background embedding and indexing."""
        if not self._pipeline:
            return
        from velune.memory.embedding_pipeline import EmbedQueueItem

        role = getattr(turn, "role", "unknown")
        self._pipeline.enqueue(
            EmbedQueueItem(
                record_id=f"mem-{uuid.uuid4().hex[:12]}",
                turn_id=getattr(turn, "id", ""),
                session_id=getattr(turn, "session_id", ""),
                role=role,
                content=getattr(turn, "content", ""),
                source_type=f"turn_{role}",
                workspace_root=workspace_root,
                created_at=getattr(turn, "created_at", time.time()),
            )
        )

    async def index_session_summary(
        self,
        session_id: str,
        summary: str,
        workspace_root: str = "",
    ) -> None:
        """Non-blocking: enqueue a session summary for background embedding."""
        if not self._pipeline:
            return
        from velune.memory.embedding_pipeline import EmbedQueueItem

        self._pipeline.enqueue(
            EmbedQueueItem(
                record_id=f"sum-{uuid.uuid4().hex[:12]}",
                turn_id="",
                session_id=session_id,
                role="system",
                content=summary,
                source_type="session_summary",
                workspace_root=workspace_root,
                created_at=time.time(),
            )
        )

    # ── Maintenance ────────────────────────────────────────────────────────────

    async def prune_low_vitality(self, threshold: float = 0.2) -> int:
        """Delete stored entries whose trust_score is below *threshold*."""
        if not self._store:
            return 0
        count = await self._store.prune_by_trust(threshold)
        if count:
            logger.info("SemanticMemory pruned %d low-vitality entries", count)
        return count
