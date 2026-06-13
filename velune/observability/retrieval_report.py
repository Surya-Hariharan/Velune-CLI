"""Turn a real retrieval run into an inspectable trace report.

``velune retrieval trace`` answers the suspicion: *does retrieval actually run,
or is it a stub?* It executes a genuine query through the live
:class:`~velune.retrieval.hybrid.HybridRetriever` and this module renders the
outcome — which sub-retrievers fired, how long each took, how many candidates
they produced, and the final ranked hits.

Every number originates from ``result.metadata["diagnostics"]``, which the
retriever measures during the run (see
:meth:`velune.retrieval.hybrid.HybridRetriever.retrieve`). This module adds no
statistics of its own; it only reshapes and redacts. Hit snippets are passed
through :func:`velune.core.redaction.redact_secrets` so the trace never leaks a
credential that happened to be indexed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from velune.core.redaction import redact_secrets

if TYPE_CHECKING:
    from velune.retrieval.schemas import RetrievalResult

_SNIPPET_LEN = 90


@dataclass
class StageStat:
    """Measured outcome of one retrieval sub-stage."""

    name: str
    enabled: bool
    hits: int
    ms: float
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "hits": self.hits,
            "ms": round(self.ms, 2),
            "note": self.note,
        }


@dataclass
class HitView:
    """A single ranked hit, reshaped for display (snippet redacted)."""

    rank: int
    score: float
    source: str
    doc_id: str
    label: str
    snippet: str

    def to_dict(self) -> dict:
        return {
            "rank": self.rank,
            "score": round(self.score, 4),
            "source": self.source,
            "doc_id": self.doc_id,
            "label": self.label,
            "snippet": self.snippet,
        }


@dataclass
class RetrievalTraceReport:
    """A fully-derived, redacted view of one retrieval run."""

    query: str
    top_k: int
    strategy: str
    total_ms: float
    embedding_available: bool | None
    stages: list[StageStat]
    hits: list[HitView]
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "top_k": self.top_k,
            "strategy": self.strategy,
            "total_ms": round(self.total_ms, 2),
            "embedding_available": self.embedding_available,
            "stages": [s.to_dict() for s in self.stages],
            "hits": [h.to_dict() for h in self.hits],
            "notes": self.notes,
        }


def _snippet(text: str) -> str:
    """Collapse whitespace, redact secrets, and truncate a hit's content."""
    if not text:
        return ""
    collapsed = " ".join(text.split())
    redacted = redact_secrets(collapsed)
    if len(redacted) > _SNIPPET_LEN:
        return redacted[:_SNIPPET_LEN].rstrip() + "…"
    return redacted


def _label(metadata: dict, doc_id: str) -> str:
    """Pick the most human-meaningful identifier for a hit."""
    for key in ("path", "file_path", "name", "qualified_name", "title"):
        value = metadata.get(key)
        if value:
            return str(value)
    return doc_id


def build_retrieval_trace(result: RetrievalResult) -> RetrievalTraceReport:
    """Reshape a :class:`RetrievalResult` into a redacted, displayable report."""
    diag = dict(result.metadata.get("diagnostics", {}))

    def stage(name: str, label: str, note: str = "") -> StageStat:
        raw = diag.get(name, {})
        return StageStat(
            name=label,
            enabled=bool(raw.get("enabled", False)),
            hits=int(raw.get("hits", 0)),
            ms=float(raw.get("ms", 0.0)),
            note=note,
        )

    vector_raw = diag.get("vector", {})
    embedding_available = vector_raw.get("embedding_available")
    graph_seeds = diag.get("graph", {}).get("seeds", 0)
    fusion_raw = diag.get("fusion", {})
    rerank_raw = diag.get("rerank", {})

    stages = [
        stage("lexical", "Lexical (BM25)"),
        stage(
            "vector",
            "Vector (embedding)",
            note=(
                ""
                if embedding_available is None
                else ("embedding ready" if embedding_available else "no embedding available")
            ),
        ),
        stage("graph", "Graph traversal", note=f"{graph_seeds} seed(s)" if graph_seeds else ""),
        StageStat(
            name="Fusion (dedup)",
            enabled=True,
            hits=int(fusion_raw.get("candidates", 0)),
            ms=float(fusion_raw.get("ms", 0.0)),
            note="merged candidates",
        ),
        StageStat(
            name="Rerank",
            enabled=True,
            hits=int(rerank_raw.get("out", 0)),
            ms=float(rerank_raw.get("ms", 0.0)),
            note=f"{rerank_raw.get('in', 0)} → {rerank_raw.get('out', 0)}",
        ),
    ]

    hits = [
        HitView(
            rank=i,
            score=float(hit.score),
            source=str(hit.source),
            doc_id=hit.document.id,
            label=_label(hit.document.metadata, hit.document.id),
            snippet=_snippet(hit.document.content),
        )
        for i, hit in enumerate(result.hits, 1)
    ]

    notes: list[str] = []
    if not diag:
        notes.append(
            "No diagnostics recorded — this retriever did not report per-stage timing."
        )
    if embedding_available is False:
        notes.append(
            "Vector search was skipped: no embedding backend is available. "
            "Results came from lexical/graph retrieval only."
        )
    if not hits:
        notes.append(
            "No hits. The retrieval indexes may be empty for this workspace — "
            "run a session or index content before tracing."
        )

    return RetrievalTraceReport(
        query=result.query.text,
        top_k=result.query.top_k,
        strategy=result.strategy,
        total_ms=float(diag.get("total_ms", 0.0)),
        embedding_available=embedding_available,
        stages=stages,
        hits=hits,
        notes=notes,
    )
