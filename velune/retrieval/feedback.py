"""Retrieval feedback recording (Phase 4, Part 9) — records outcomes, does not learn from them.

After every turn, records what was retrieved, what survived context-budget
trimming, and how much of the token budget was used — through the existing
structured-logging path (``velune/telemetry/``), not a new storage system.

Deliberately scoped to *recording*: nothing here re-tunes fusion weights,
reranker trust scores, or the intent→strategy table automatically. Turning
this data into closed-loop auto-tuning is real, separate work — see
``docs/PHASE4_INTELLIGENT_RETRIEVAL.md`` — and isn't attempted here.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from velune.context.sections import ContextAssemblyReport

logger = structlog.get_logger("velune.retrieval.feedback")

_DEFAULT_MAX_HISTORY = 50


@dataclass
class RetrievalFeedbackEntry:
    """One turn's retrieval-and-assembly outcome."""

    timestamp: float
    query_text: str
    intent: str
    confidence: float
    hit_counts_by_source: dict[str, int] = field(default_factory=dict)
    chunks_kept: int = 0
    chunks_dropped: int = 0
    tokens_assembled: int = 0
    tokens_requested: int = 0
    budget_exceeded: bool = False


class RetrievalFeedbackRecorder:
    """Bounded in-memory history of per-turn retrieval outcomes, mirrored to structured logs.

    One instance is meant to live for the session (registered in the DI
    container, like :class:`~velune.retrieval.planner.RetrievalPlanner`) so
    ``recent()``/``summary()`` reflect the whole session, not just one turn.
    """

    def __init__(self, max_history: int = _DEFAULT_MAX_HISTORY) -> None:
        self._history: deque[RetrievalFeedbackEntry] = deque(maxlen=max_history)

    def record(
        self,
        *,
        query_text: str,
        intent: str,
        confidence: float,
        hit_counts_by_source: dict[str, int],
        report: ContextAssemblyReport,
    ) -> RetrievalFeedbackEntry:
        """Record one turn's outcome from its already-computed ``ContextAssemblyReport``."""
        entry = RetrievalFeedbackEntry(
            timestamp=time.time(),
            query_text=query_text,
            intent=intent,
            confidence=confidence,
            hit_counts_by_source=dict(hit_counts_by_source),
            chunks_kept=max(0, report.total_chunks_received - report.chunks_dropped),
            chunks_dropped=report.chunks_dropped,
            tokens_assembled=report.total_tokens_assembled,
            tokens_requested=report.total_tokens_requested,
            budget_exceeded=report.budget_exceeded,
        )
        self._history.append(entry)
        logger.info(
            "retrieval_feedback",
            query=query_text[:200],
            intent=intent,
            confidence=confidence,
            hit_counts_by_source=entry.hit_counts_by_source,
            chunks_kept=entry.chunks_kept,
            chunks_dropped=entry.chunks_dropped,
            tokens_assembled=entry.tokens_assembled,
            tokens_requested=entry.tokens_requested,
            budget_exceeded=entry.budget_exceeded,
        )
        return entry

    def recent(self, limit: int = 10) -> list[RetrievalFeedbackEntry]:
        """The most recent *limit* recorded entries, newest last."""
        if limit <= 0:
            return []
        return list(self._history)[-limit:]

    def summary(self) -> dict[str, Any]:
        """Aggregate stats over the recorded (bounded) history."""
        total = len(self._history)
        if total == 0:
            return {"turns_recorded": 0}
        avg_kept = sum(e.chunks_kept for e in self._history) / total
        avg_dropped = sum(e.chunks_dropped for e in self._history) / total
        exceeded = sum(1 for e in self._history if e.budget_exceeded)
        return {
            "turns_recorded": total,
            "avg_chunks_kept": round(avg_kept, 2),
            "avg_chunks_dropped": round(avg_dropped, 2),
            "budget_exceeded_rate": round(exceeded / total, 2),
        }


def hit_counts_by_source(chunk_sources: list[str]) -> dict[str, int]:
    """Tally retrieval-source labels like ``"hybrid_retrieval:lexical"`` into ``{"lexical": 2, ...}``.

    Only counts sources carrying a ``prefix:source`` shape (the convention
    ``prompt_context.py`` uses for hybrid-retrieval/three-brain chunks);
    sources without a colon (e.g. ``"session"``, ``"user"``) are skipped —
    they aren't retrieval hits.
    """
    counts: dict[str, int] = {}
    for source in chunk_sources:
        if ":" not in source:
            continue
        _, _, label = source.rpartition(":")
        counts[label] = counts.get(label, 0) + 1
    return counts
