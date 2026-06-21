"""Cross-encoder style reranking (simple scoring for Phase 2a).

The reranker is shared by two callers that pass differently-shaped objects:

* :class:`~velune.retrieval.pipeline.RetrievalPipeline` passes mutable
  ``ContextChunk`` dataclasses (``.relevance_score``, ``.content``, ``.source``);
  downstream orchestration reads the ``.combined_score`` this writes back.
* :class:`~velune.retrieval.hybrid.HybridRetriever` passes immutable
  ``RetrievalHit`` pydantic models (``.score``, ``.document.content``).

To serve both without crashing, scoring reads through duck-typed accessors and
ranking is driven by a per-call score map rather than by mutating the inputs.
``combined_score`` is still written back when the object accepts it, preserving
the ``ContextChunk`` contract.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("velune.retrieval.reranker")


def _relevance(chunk: Any) -> float:
    """Relevance signal: ``relevance_score`` (ContextChunk) or ``score`` (RetrievalHit)."""
    value = getattr(chunk, "relevance_score", None)
    if value is None:
        value = getattr(chunk, "score", None)
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _content(chunk: Any) -> str:
    """Text content: ``chunk.content`` or ``chunk.document.content``."""
    content = getattr(chunk, "content", None)
    if content is None:
        document = getattr(chunk, "document", None)
        content = getattr(document, "content", None) if document is not None else None
    return str(content) if content is not None else ""


def _metadata(chunk: Any) -> dict[str, Any]:
    """Metadata: ``chunk.metadata`` or ``chunk.document.metadata``."""
    metadata = getattr(chunk, "metadata", None)
    if metadata is None:
        document = getattr(chunk, "document", None)
        metadata = getattr(document, "metadata", None) if document is not None else None
    return metadata if isinstance(metadata, dict) else {}


def _source(chunk: Any) -> str:
    """Source label as a plain string (handles str and StrEnum)."""
    return str(getattr(chunk, "source", "") or "")


class CrossEncoderReranker:
    """Reranks retrieved chunks using combined scoring.

    Phase 2a: Simple score formula (no ML model)
    Phase 3+: Real cross-encoder when compute available
    """

    # Score weights
    SEMANTIC_WEIGHT = 0.5
    RECENCY_WEIGHT = 0.3
    TRUST_WEIGHT = 0.2

    # Thresholds
    MIN_RECENCY_SECONDS = 300
    MAX_RECENCY_SECONDS = 2592000

    def __init__(self) -> None:
        """Initialize reranker."""
        pass

    def rerank(
        self,
        chunks: list[Any],
        query: str | None = None,
    ) -> list[Any]:
        """Rerank chunks by combined score, tolerant of both chunk shapes."""
        start_time = time.perf_counter()

        # Score into a per-call map keyed by identity so ranking never depends on
        # mutating the (possibly immutable) input objects.
        scores: dict[int, float] = {}
        for chunk in chunks:
            score = self._calculate_combined_score(chunk)
            scores[id(chunk)] = score
            # Write back for callers (ContextChunk) whose downstream reads it.
            try:
                chunk.combined_score = score
            except (AttributeError, ValueError, TypeError):
                pass  # immutable RetrievalHit — score lives in the map instead

        ranked = sorted(chunks, key=lambda c: scores[id(c)], reverse=True)
        deduplicated = self._deduplicate_by_content(ranked, scores)

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        logger.debug(f"Reranking {len(chunks)} → {len(deduplicated)} in {elapsed_ms:.1f}ms")

        return deduplicated

    def _calculate_combined_score(self, chunk: Any) -> float:
        """Calculate combined score."""
        semantic = min(1.0, max(0.0, _relevance(chunk)))
        recency = self._calculate_recency_score(chunk)
        trust = self._calculate_trust_score(chunk)

        combined = (
            (semantic * self.SEMANTIC_WEIGHT)
            + (recency * self.RECENCY_WEIGHT)
            + (trust * self.TRUST_WEIGHT)
        )

        return min(1.0, max(0.0, combined))

    def _calculate_recency_score(self, chunk: Any) -> float:
        """Calculate recency score."""
        raw_timestamp = _metadata(chunk).get("timestamp")
        if not raw_timestamp:
            return 0.5
        try:
            timestamp = float(raw_timestamp)
        except (TypeError, ValueError):
            return 0.5

        now = time.time()
        age_seconds = now - timestamp

        if age_seconds < self.MIN_RECENCY_SECONDS:
            return 1.0
        if age_seconds > self.MAX_RECENCY_SECONDS:
            return 0.0

        age_range = self.MAX_RECENCY_SECONDS - self.MIN_RECENCY_SECONDS
        score = 1.0 - ((age_seconds - self.MIN_RECENCY_SECONDS) / age_range)
        return min(1.0, max(0.0, score))

    def _calculate_trust_score(self, chunk: Any) -> float:
        """Calculate trust score based on source."""
        source_trust = {
            "symbol": 0.9,
            "import_graph": 0.85,
            "lineage": 0.8,
            "call_graph": 0.75,
            "episodic": 0.7,
            "semantic": 0.6,
        }
        return source_trust.get(_source(chunk), 0.5)

    def _deduplicate_by_content(self, chunks: list[Any], scores: dict[int, float]) -> list[Any]:
        """Remove similar chunks keeping highest score."""
        seen: dict[str, Any] = {}
        for chunk in chunks:
            content_sig = _content(chunk)[:100].lower()
            if content_sig not in seen or scores[id(chunk)] > scores[id(seen[content_sig])]:
                seen[content_sig] = chunk
        return list(seen.values())
