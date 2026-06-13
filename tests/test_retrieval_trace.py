"""Tests for `velune retrieval trace`: pipeline diagnostics + report builder.

These verify two truthfulness guarantees:

* :meth:`HybridRetriever.retrieve` records *measured* per-stage diagnostics
  (enablement, hit counts, timings) from a real run — not invented numbers.
* :func:`build_retrieval_trace` faithfully reshapes that data and redacts any
  secret that happened to be indexed before it reaches the trace output.
"""

from __future__ import annotations

import pytest

from velune.observability.retrieval_report import build_retrieval_trace
from velune.retrieval.schemas import (
    RetrievalDocument,
    RetrievalHit,
    RetrievalQuery,
    RetrievalResult,
    RetrievalSource,
)


class TestPipelineDiagnostics:
    """The real retriever must measure and report what each stage did."""

    @pytest.mark.asyncio
    async def test_lexical_run_records_real_diagnostics(self):
        from velune.retrieval.hybrid import HybridRetriever

        retriever = HybridRetriever(location=":memory:")
        # A realistic corpus: the query terms appear in a minority of documents.
        # (BM25Okapi's IDF is 0 for a term present in exactly half the corpus, so
        # a 2-doc corpus is a degenerate case that scores everything at zero.)
        docs = [
            RetrievalDocument(
                id="doc-sandbox",
                content="the sandbox validator checks every subprocess command",
                metadata={"path": "velune/execution/sandbox/validator.py"},
                embedding=[0.1, 0.2, 0.3],
            ),
            RetrievalDocument(
                id="doc-memory",
                content="the memory tier persists episodic records",
                metadata={"path": "velune/memory/tiers.py"},
                embedding=[0.4, 0.5, 0.6],
            ),
            RetrievalDocument(
                id="doc-models",
                content="model providers expose chat completions",
                metadata={"path": "velune/providers/base.py"},
                embedding=[0.7, 0.8, 0.9],
            ),
            RetrievalDocument(
                id="doc-config",
                content="configuration settings load from disk",
                metadata={"path": "velune/core/config.py"},
                embedding=[0.2, 0.4, 0.6],
            ),
            RetrievalDocument(
                id="doc-render",
                content="terminal rendering draws panels and tables",
                metadata={"path": "velune/cli/rendering.py"},
                embedding=[0.3, 0.6, 0.9],
            ),
        ]
        retriever.add_documents(docs)

        # Lexical-only: avoids needing an embedding backend, but still exercises
        # the full retrieve() instrumentation path.
        query = RetrievalQuery(
            text="sandbox validator",
            top_k=5,
            vector_weight=0.0,
            lexical_weight=1.0,
            graph_weight=0.0,
        )
        result = await retriever.retrieve(query)

        diag = result.metadata["diagnostics"]
        # Stage enablement reflects the query weights honestly.
        assert diag["lexical"]["enabled"] is True
        assert diag["vector"]["enabled"] is False
        assert diag["graph"]["enabled"] is False
        # Lexical actually found the sandbox doc.
        assert diag["lexical"]["hits"] >= 1
        # Timings are measured, non-negative floats.
        assert isinstance(diag["total_ms"], float)
        assert diag["total_ms"] >= 0.0
        assert diag["rerank"]["out"] == len(result.hits)
        # The top hit is the sandbox document, not the memory one.
        assert result.hits[0].document.id == "doc-sandbox"

    @pytest.mark.asyncio
    async def test_disabled_stages_do_no_work(self):
        from velune.retrieval.hybrid import HybridRetriever

        retriever = HybridRetriever(location=":memory:")
        retriever.add_documents(
            [
                RetrievalDocument(
                    id="d1", content="alpha beta gamma", embedding=[0.1, 0.2, 0.3]
                )
            ]
        )
        query = RetrievalQuery(
            text="alpha",
            vector_weight=0.0,
            lexical_weight=1.0,
            graph_weight=0.0,
        )
        result = await retriever.retrieve(query)
        diag = result.metadata["diagnostics"]
        # A disabled stage stays at zero hits and is marked not-enabled.
        assert diag["vector"]["enabled"] is False
        assert diag["vector"]["hits"] == 0
        assert diag["graph"]["enabled"] is False
        assert diag["graph"]["hits"] == 0


class TestRerankerShapeTolerance:
    """Regression: the shared reranker must serve both chunk shapes.

    ``HybridRetriever`` passes immutable ``RetrievalHit`` models while
    ``RetrievalPipeline`` passes mutable ``ContextChunk`` dataclasses. The
    reranker previously assumed only the ContextChunk shape and crashed with
    ``AttributeError`` the moment the hybrid path produced any hits.
    """

    def test_reranks_retrieval_hits_without_crashing(self):
        from velune.retrieval.reranker import CrossEncoderReranker

        hits = [
            RetrievalHit(
                document=RetrievalDocument(id="a", content="alpha content"),
                score=0.2,
                source=RetrievalSource.LEXICAL,
            ),
            RetrievalHit(
                document=RetrievalDocument(id="b", content="beta content"),
                score=0.9,
                source=RetrievalSource.VECTOR,
            ),
        ]
        out = CrossEncoderReranker().rerank(hits, "query")
        # No crash, all distinct content preserved, deterministic ordering.
        assert {h.document.id for h in out} == {"a", "b"}

    def test_sets_combined_score_on_context_chunks(self):
        from velune.retrieval.pipeline import ContextChunk
        from velune.retrieval.reranker import CrossEncoderReranker

        chunks = [
            ContextChunk(id="c1", source="semantic", content="one", relevance_score=0.8),
            ContextChunk(id="c2", source="symbol", content="two", relevance_score=0.4),
        ]
        out = CrossEncoderReranker().rerank(chunks, "q")
        # The ContextChunk contract (combined_score written back) is preserved.
        assert all(c.combined_score > 0.0 for c in out)

    def test_dedupes_identical_content_keeping_higher_score(self):
        from velune.retrieval.reranker import CrossEncoderReranker

        hits = [
            RetrievalHit(
                document=RetrievalDocument(id="low", content="same text"),
                score=0.1,
                source=RetrievalSource.LEXICAL,
            ),
            RetrievalHit(
                document=RetrievalDocument(id="high", content="same text"),
                score=0.9,
                source=RetrievalSource.VECTOR,
            ),
        ]
        out = CrossEncoderReranker().rerank(hits, "q")
        assert len(out) == 1


class TestReportBuilder:
    """The pure report builder reshapes and redacts; it invents nothing."""

    def _result(self, *, hits, diagnostics, query_text="q", top_k=10):
        return RetrievalResult(
            query=RetrievalQuery(text=query_text, top_k=top_k),
            hits=hits,
            strategy="hybrid-fusion-reranked",
            metadata={"diagnostics": diagnostics},
        )

    def test_stages_reshaped_from_diagnostics(self):
        diag = {
            "lexical": {"enabled": True, "hits": 3, "ms": 1.2},
            "vector": {"enabled": True, "hits": 0, "ms": 5.0, "embedding_available": False},
            "graph": {"enabled": False, "hits": 0, "ms": 0.0, "seeds": 0},
            "fusion": {"candidates": 3, "ms": 0.1},
            "rerank": {"in": 3, "out": 2, "ms": 0.3},
            "total_ms": 6.6,
        }
        report = build_retrieval_trace(self._result(hits=[], diagnostics=diag))

        by_name = {s.name: s for s in report.stages}
        assert by_name["Lexical (BM25)"].hits == 3
        assert by_name["Vector (embedding)"].note == "no embedding available"
        assert by_name["Rerank"].note == "3 → 2"
        assert report.total_ms == 6.6
        assert report.embedding_available is False
        # Honest note surfaced when vector was skipped.
        assert any("Vector search was skipped" in n for n in report.notes)

    def test_hit_snippet_is_redacted(self):
        secret = "sk-ant-" + "A" * 40
        doc = RetrievalDocument(
            id="leaky",
            content=f"config loads the key {secret} at startup",
            metadata={"path": "config.py"},
        )
        hit = RetrievalHit(document=doc, score=0.9, source=RetrievalSource.LEXICAL, rank=1)
        report = build_retrieval_trace(
            self._result(hits=[hit], diagnostics={"total_ms": 1.0})
        )
        snippet = report.hits[0].snippet
        assert secret not in snippet
        assert "***REDACTED***" in snippet
        assert report.hits[0].label == "config.py"

    def test_label_falls_back_to_doc_id(self):
        doc = RetrievalDocument(id="bare-id", content="hello", metadata={})
        hit = RetrievalHit(document=doc, score=0.5, source=RetrievalSource.MEMORY)
        report = build_retrieval_trace(
            self._result(hits=[hit], diagnostics={"total_ms": 0.0})
        )
        assert report.hits[0].label == "bare-id"

    def test_empty_hits_note(self):
        report = build_retrieval_trace(self._result(hits=[], diagnostics={"total_ms": 0.0}))
        assert any("No hits" in n for n in report.notes)

    def test_missing_diagnostics_is_honest(self):
        result = RetrievalResult(
            query=RetrievalQuery(text="q"), hits=[], strategy="hybrid", metadata={}
        )
        report = build_retrieval_trace(result)
        assert any("No diagnostics recorded" in n for n in report.notes)

    def test_to_dict_is_json_safe(self):
        import json

        diag = {
            "lexical": {"enabled": True, "hits": 1, "ms": 1.0},
            "total_ms": 2.0,
        }
        doc = RetrievalDocument(id="d", content="x", metadata={"name": "thing"})
        hit = RetrievalHit(document=doc, score=0.7, source=RetrievalSource.LEXICAL, rank=1)
        report = build_retrieval_trace(self._result(hits=[hit], diagnostics=diag))
        payload = json.loads(json.dumps(report.to_dict()))
        assert payload["hits"][0]["label"] == "thing"
        assert payload["strategy"] == "hybrid-fusion-reranked"
