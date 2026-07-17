"""Tests for RetrievalPlanner (Phase 4, Part 3).

Covers: intent-conditioned strategy selection, the low-confidence fallback,
config-sourced defaults (finally wiring the previously-dead
kernel/config.py::RetrievalConfig into something real), and the bounded
exact-key result cache.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from velune.cognition.intent import IntentType
from velune.retrieval.planner import RetrievalPlanner
from velune.retrieval.schemas import RetrievalDocument, RetrievalHit, RetrievalResult, RetrievalSource


class TestPlan:
    def test_dependency_analysis_leans_on_the_graph(self):
        query = RetrievalPlanner().plan(IntentType.DEPENDENCY_ANALYSIS, 0.9, "what imports this")
        assert query.graph_weight > query.vector_weight
        assert query.graph_weight > query.lexical_weight

    def test_search_leans_lexical(self):
        query = RetrievalPlanner().plan(IntentType.SEARCH, 0.9, "find the sandbox validator")
        assert query.lexical_weight > query.vector_weight
        assert query.lexical_weight > query.graph_weight

    def test_generate_leans_semantic(self):
        query = RetrievalPlanner().plan(IntentType.GENERATE, 0.9, "write a new handler")
        assert query.vector_weight > query.lexical_weight
        assert query.vector_weight > query.graph_weight

    def test_weights_are_always_valid_fractions(self):
        planner = RetrievalPlanner()
        for intent in IntentType:
            query = planner.plan(intent, 0.9, "text")
            assert 0.0 <= query.vector_weight <= 1.0
            assert 0.0 <= query.lexical_weight <= 1.0
            assert 0.0 <= query.graph_weight <= 1.0
            assert 1 <= query.top_k <= 100

    def test_intent_is_stamped_onto_the_query(self):
        query = RetrievalPlanner().plan(IntentType.SECURITY, 0.9, "check for xss")
        assert query.intent == IntentType.SECURITY.value

    def test_low_confidence_falls_back_to_balanced_default(self):
        """A skewed strategy on a guess costs more recall than the balanced
        default — low confidence must not commit to e.g. an all-graph query
        for what might not actually be a DEPENDENCY_ANALYSIS turn."""
        low = RetrievalPlanner().plan(IntentType.DEPENDENCY_ANALYSIS, 0.1, "text")
        assert (low.vector_weight, low.lexical_weight, low.graph_weight) == (0.5, 0.3, 0.2)

    def test_config_overrides_the_default_fallback(self):
        config = SimpleNamespace(
            vector_weight=0.1, lexical_weight=0.1, graph_weight=0.8, rerank_top_k=7
        )
        planner = RetrievalPlanner(config=config)
        # Low confidence uses the fallback strategy, which should now be config-sourced.
        query = planner.plan(IntentType.QUESTION, 0.1, "text")
        assert (query.vector_weight, query.lexical_weight, query.graph_weight) == (0.1, 0.1, 0.8)
        assert query.top_k == 7

    def test_malformed_config_is_ignored_not_fatal(self):
        """A config-shaped object missing expected attributes must not crash
        planner construction — fall back to the historical hardcoded default."""
        planner = RetrievalPlanner(config=object())
        query = planner.plan(IntentType.QUESTION, 0.1, "text")
        assert (query.vector_weight, query.lexical_weight, query.graph_weight) == (0.5, 0.3, 0.2)


class TestResultCache:
    def _result(self, doc_id: str = "a") -> RetrievalResult:
        from velune.retrieval.schemas import RetrievalQuery

        hit = RetrievalHit(
            document=RetrievalDocument(id=doc_id, content="x"),
            score=0.9,
            source=RetrievalSource.LEXICAL,
            rank=1,
        )
        return RetrievalResult(query=RetrievalQuery(text="q"), hits=[hit])

    def test_get_cached_is_none_before_any_put(self):
        planner = RetrievalPlanner()
        query = planner.plan(IntentType.QUESTION, 0.9, "q")
        assert planner.get_cached(query) is None

    def test_put_then_get_returns_the_same_result(self):
        planner = RetrievalPlanner()
        query = planner.plan(IntentType.QUESTION, 0.9, "q")
        result = self._result()
        planner.put_cached(query, result)
        assert planner.get_cached(query) is result

    def test_different_text_is_a_cache_miss(self):
        planner = RetrievalPlanner()
        q1 = planner.plan(IntentType.QUESTION, 0.9, "q1")
        q2 = planner.plan(IntentType.QUESTION, 0.9, "q2")
        planner.put_cached(q1, self._result())
        assert planner.get_cached(q2) is None

    def test_cache_expires_after_ttl(self, monkeypatch):
        import velune.retrieval.planner as planner_mod

        planner = RetrievalPlanner()
        query = planner.plan(IntentType.QUESTION, 0.9, "q")
        planner.put_cached(query, self._result())

        real_time = planner_mod.time.time
        monkeypatch.setattr(
            planner_mod.time, "time", lambda: real_time() + planner_mod._CACHE_TTL_SECONDS + 1
        )
        assert planner.get_cached(query) is None

    def test_cache_is_bounded(self):
        planner = RetrievalPlanner()
        for i in range(planner_max_entries() + 5):
            query = planner.plan(IntentType.QUESTION, 0.9, f"query {i}")
            planner.put_cached(query, self._result())
        assert len(planner._cache) <= planner_max_entries()

    async def test_plan_and_retrieve_uses_the_cache_on_a_repeat_query(self):
        planner = RetrievalPlanner()
        retriever = SimpleNamespace(retrieve=AsyncMock(return_value=self._result()))

        first = await planner.plan_and_retrieve(retriever, IntentType.SEARCH, 0.9, "find x")
        second = await planner.plan_and_retrieve(retriever, IntentType.SEARCH, 0.9, "find x")

        assert first is second
        retriever.retrieve.assert_awaited_once()


def planner_max_entries() -> int:
    from velune.retrieval.planner import _CACHE_MAX_ENTRIES

    return _CACHE_MAX_ENTRIES
