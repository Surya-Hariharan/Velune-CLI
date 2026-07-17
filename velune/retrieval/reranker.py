"""Cross-encoder style reranking (simple scoring for Phase 2a).

Scoring reads through duck-typed accessors so it can rerank the
:class:`~velune.retrieval.hybrid.HybridRetriever` caller's immutable
``RetrievalHit`` pydantic models (``.score``, ``.document.content``) without
assuming a single concrete input shape.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from velune.cognition.intent import IntentType

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

    # Additive trust boosts for a source when it matches the query's intent —
    # on top of the base per-source trust table below. Small and additive
    # (not a multiplier) so ranking stays intent-*aware* rather than
    # intent-*dominated*: a highly relevant hit from an unboosted source can
    # still outrank a barely-relevant hit from a boosted one.
    # Keyed by the actual ``RetrievalSource`` labels that ``HybridRetriever``
    # emits — ``lexical`` / ``vector`` / ``graph`` / ``memory`` — with the older
    # specialized labels (``import_graph`` / ``call_graph`` / ``symbol``) kept so
    # any caller still using them is unaffected.
    INTENT_TRUST_BOOST: dict[str, dict[str, float]] = {
        "architecture": {"graph": 0.1, "import_graph": 0.1, "call_graph": 0.1},
        "dependency_analysis": {"graph": 0.15, "import_graph": 0.15, "call_graph": 0.1},
        "refactor": {"graph": 0.05, "symbol": 0.1, "call_graph": 0.05},
        "debug": {"graph": 0.1, "vector": 0.05, "symbol": 0.05, "call_graph": 0.1},
        "security": {"graph": 0.05, "lexical": 0.05, "call_graph": 0.05},
        "search": {"vector": 0.1, "lexical": 0.1},
        "documentation": {"vector": 0.1},
        "test_generation": {"vector": 0.05, "lexical": 0.05},
    }

    def __init__(self) -> None:
        """Initialize reranker."""
        pass

    def rerank(
        self,
        chunks: list[Any],
        query: str | None = None,
        intent: IntentType | str | None = None,
    ) -> list[Any]:
        """Rerank chunks by combined score, tolerant of both chunk shapes.

        *intent*, when given, nudges the trust component for sources that
        matter most for that intent (e.g. the import graph for
        DEPENDENCY_ANALYSIS) — see ``INTENT_TRUST_BOOST``. Unlisted intents
        (and ``None``) behave exactly as before this parameter existed.
        """
        start_time = time.perf_counter()
        intent_value = getattr(intent, "value", intent)

        # Score into a per-call map keyed by identity so ranking never depends on
        # mutating the (possibly immutable) input objects.
        scores: dict[int, float] = {}
        for chunk in chunks:
            score = self._calculate_combined_score(chunk, intent_value)
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

    def _calculate_combined_score(self, chunk: Any, intent_value: str | None = None) -> float:
        """Calculate combined score."""
        semantic = min(1.0, max(0.0, _relevance(chunk)))
        recency = self._calculate_recency_score(chunk)
        trust = self._calculate_trust_score(chunk, intent_value)

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

    def _calculate_trust_score(self, chunk: Any, intent_value: str | None = None) -> float:
        """Calculate trust score based on source, nudged by query intent."""
        source_trust = {
            # Real hybrid-retrieval source labels (RetrievalSource values). These
            # are what actually reach the reranker; keying on the old vocabulary
            # left every hybrid hit at the 0.5 default, making the trust term
            # inert.
            "graph": 0.85,  # structural dependency graph
            "vector": 0.7,  # semantic embedding match
            "memory": 0.7,  # working / episodic / semantic memory
            "lexical": 0.6,  # BM25 keyword match
            # Legacy / specialized labels retained for other callers.
            "symbol": 0.9,
            "import_graph": 0.85,
            "lineage": 0.8,
            "call_graph": 0.75,
            "episodic": 0.7,
            "semantic": 0.6,
        }
        source = _source(chunk)
        base = source_trust.get(source, 0.5)
        boost = self.INTENT_TRUST_BOOST.get(intent_value or "", {}).get(source, 0.0)
        return min(1.0, base + boost)

    def _deduplicate_by_content(self, chunks: list[Any], scores: dict[int, float]) -> list[Any]:
        """Remove similar chunks keeping highest score."""
        seen: dict[str, Any] = {}
        for chunk in chunks:
            content_sig = _content(chunk)[:100].lower()
            if content_sig not in seen or scores[id(chunk)] > scores[id(seen[content_sig])]:
                seen[content_sig] = chunk
        return list(seen.values())
