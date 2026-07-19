"""Tests for HybridRetriever's per-source score normalization (Phase 4, Part 5).

Before this, `_run` fused raw lexical (BM25, unbounded — can run into the
tens for a strong match), vector (cosine, ~[0,1]), and graph (a fixed
~0.9-ish heuristic) scores via a plain weighted sum with no rescaling. BM25's
larger absolute magnitude silently dominated fusion regardless of the
configured lexical_weight. These tests pin the fix: each source is min-max
normalized to [0,1] *within its own result set* before the weighted sum.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from velune.retrieval.hybrid import HybridRetriever, _normalize_scores
from velune.retrieval.schemas import (
    RetrievalDocument,
    RetrievalHit,
    RetrievalQuery,
    RetrievalSource,
)


def _hit(score: float, source: RetrievalSource, doc_id: str = "d") -> RetrievalHit:
    return RetrievalHit(
        document=RetrievalDocument(id=doc_id, content=f"content {doc_id}"),
        score=score,
        source=source,
        rank=1,
    )


class TestNormalizeScores:
    def test_empty_list_is_unchanged(self):
        assert _normalize_scores([]) == []

    def test_single_hit_is_unchanged(self):
        """Min-max normalization is undefined with no spread — leave it as-is
        rather than collapsing a lone hit's score to an arbitrary 0 or 1."""
        hits = [_hit(7.3, RetrievalSource.LEXICAL)]
        out = _normalize_scores(hits)
        assert out[0].score == 7.3

    def test_all_equal_scores_are_unchanged(self):
        hits = [_hit(2.0, RetrievalSource.LEXICAL, "a"), _hit(2.0, RetrievalSource.LEXICAL, "b")]
        out = _normalize_scores(hits)
        assert [h.score for h in out] == [2.0, 2.0]

    def test_min_max_normalizes_to_unit_range(self):
        hits = [
            _hit(15.2, RetrievalSource.LEXICAL, "hi"),
            _hit(3.1, RetrievalSource.LEXICAL, "mid"),
            _hit(0.0, RetrievalSource.LEXICAL, "lo"),
        ]
        out = _normalize_scores(hits)
        scores = {h.document.id: h.score for h in out}
        assert scores["hi"] == 1.0
        assert scores["lo"] == 0.0
        assert 0.0 < scores["mid"] < 1.0

    def test_mutates_in_place_and_returns_same_list(self):
        hits = [_hit(10.0, RetrievalSource.LEXICAL, "a"), _hit(0.0, RetrievalSource.LEXICAL, "b")]
        out = _normalize_scores(hits)
        assert out is hits


class TestHybridFusionUsesNormalizedScores:
    """Without per-source normalization, an unbounded BM25 score dominates the
    weighted sum outright, regardless of lexical_weight — these pin the fix
    at the level callers actually observe: final ranked order."""

    def _retriever_with_fixed_hits(
        self, lexical_hits: list[RetrievalHit], vector_hits: list[RetrievalHit]
    ) -> HybridRetriever:
        retriever = HybridRetriever(location=":memory:")
        retriever.lexical_retriever.retrieve = lambda *a, **kw: lexical_hits
        retriever.vector_retriever.retrieve = lambda *a, **kw: vector_hits
        retriever._generate_embedding_async = AsyncMock(return_value=[0.1] * 8)
        return retriever

    async def test_normalization_prevents_a_huge_raw_bm25_score_from_dominating(self):
        """Regression pin for the audited bug: lexical returns a numerically
        huge but *relatively weak* match (25.0, when its own best hit is
        2.0 — i.e. it's the top of its own set) alongside a document that is
        also lexical's weakest-in-set — while vector strongly prefers the
        shared document (0.95, its own top hit) over an unrelated one (0.1).

        Unnormalized: shared_doc = 2.0*0.1 (lexical) + 0.95*0.6 (vector) = 0.77,
        irrelevant_huge = 25.0*0.1 = 2.5 → irrelevant_huge would win outright,
        purely from BM25's larger absolute magnitude, despite lexical_weight
        being six times smaller than vector_weight.

        Normalized (min-max within each source): irrelevant_huge -> 1.0,
        shared_doc(lexical) -> 0.0, shared_doc(vector) -> 1.0, other -> 0.0.
        Fused: shared_doc = 0.0*0.1 + 1.0*0.6 = 0.6, irrelevant_huge = 1.0*0.1
        = 0.1 → shared_doc correctly wins.
        """
        lexical_hits = [
            _hit(25.0, RetrievalSource.LEXICAL, "irrelevant_huge"),
            _hit(2.0, RetrievalSource.LEXICAL, "shared_doc"),
        ]
        vector_hits = [
            _hit(0.95, RetrievalSource.VECTOR, "shared_doc"),
            _hit(0.1, RetrievalSource.VECTOR, "other"),
        ]
        retriever = self._retriever_with_fixed_hits(lexical_hits, vector_hits)

        query = RetrievalQuery(
            text="q", top_k=5, vector_weight=0.6, lexical_weight=0.1, graph_weight=0.0
        )
        result = await retriever.retrieve(query)

        assert result.hits[0].document.id == "shared_doc"

    async def test_weights_still_matter_after_normalization(self):
        """With both sources normalized onto [0,1] before weighting, a much
        higher weight on one source should still be able to flip the order —
        normalization removes the *scale* bug, not the weights' effect."""
        lexical_hits = [
            _hit(10.0, RetrievalSource.LEXICAL, "lexical_pick"),
            _hit(1.0, RetrievalSource.LEXICAL, "other_lexical"),
        ]
        vector_hits = [
            _hit(0.5, RetrievalSource.VECTOR, "vector_pick"),
            _hit(0.05, RetrievalSource.VECTOR, "other_vector"),
        ]
        retriever = self._retriever_with_fixed_hits(lexical_hits, vector_hits)

        query = RetrievalQuery(
            text="q", top_k=5, vector_weight=0.05, lexical_weight=0.9, graph_weight=0.0
        )
        result = await retriever.retrieve(query)

        assert result.hits[0].document.id == "lexical_pick"
