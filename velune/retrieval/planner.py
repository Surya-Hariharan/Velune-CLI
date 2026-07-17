"""Retrieval Planner — decides retrieval strategy before any retrieval runs.

Turns ``(intent, confidence, text)`` into a fully-formed :class:`RetrievalQuery`
(fusion weights + ``top_k``), instead of every call site constructing a query
with the same fixed default weights regardless of what the user is asking.
Each :class:`~velune.cognition.intent.IntentType` favors the retrieval arm
best suited to it — e.g. dependency questions lean on the import/AST graph,
generation/documentation tasks lean on semantic similarity, search/explain
tasks lean on exact lexical matches.

Also owns a small, bounded, exact-key result cache so a caller that issues
the same query twice in quick succession (e.g. the REPL tool-loop calling
``semantic_code_search`` more than once in one turn) doesn't pay for the
full hybrid pipeline twice.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from velune.cognition.intent import IntentType
from velune.retrieval.schemas import RetrievalQuery, RetrievalResult

if TYPE_CHECKING:
    from velune.retrieval.hybrid import HybridRetriever

# (vector_weight, lexical_weight, graph_weight, top_k)
# Falls back to the historical hybrid defaults (0.5 / 0.3 / 0.2, top_k=10)
# for any intent not listed here.
_DEFAULT_STRATEGY = (0.5, 0.3, 0.2, 10)

_INTENT_STRATEGY: dict[IntentType, tuple[float, float, float, int]] = {
    # Exact name/term lookups — lexical carries most of the signal.
    IntentType.SEARCH: (0.25, 0.6, 0.15, 15),
    IntentType.QUESTION: (0.4, 0.4, 0.2, 10),
    IntentType.EXPLAIN: (0.35, 0.45, 0.2, 10),
    # Structure matters more than prose similarity.
    IntentType.REFACTOR: (0.25, 0.25, 0.5, 12),
    IntentType.ARCHITECTURE: (0.2, 0.2, 0.6, 12),
    IntentType.DEPENDENCY_ANALYSIS: (0.15, 0.2, 0.65, 15),
    # Stack traces + call chains: lexical for the error text, graph for the
    # call/import path that produced it.
    IntentType.DEBUG: (0.3, 0.4, 0.3, 10),
    IntentType.SECURITY: (0.2, 0.45, 0.35, 12),
    # Semantic pattern similarity to existing code/docs dominates.
    IntentType.GENERATE: (0.55, 0.25, 0.2, 8),
    IntentType.TEST_GENERATION: (0.5, 0.3, 0.2, 8),
    IntentType.DOCUMENTATION: (0.55, 0.3, 0.15, 8),
    IntentType.REVIEW: (0.4, 0.3, 0.3, 10),
    IntentType.COMMAND: (0.3, 0.3, 0.2, 5),
}

_CACHE_TTL_SECONDS = 30.0
_CACHE_MAX_ENTRIES = 32


class RetrievalPlanner:
    """Selects a retrieval strategy from intent and offers a short-lived result cache.

    One instance is safe to share across a session — the cache is small,
    bounded, and time-limited, so staleness is never an issue in practice.
    """

    def __init__(self, config: object | None = None) -> None:
        """*config*, if given, should be a ``RetrievalConfig``-shaped object
        (``vector_weight``/``lexical_weight``/``graph_weight``/``rerank_top_k``)
        used as the fallback strategy for intents not in ``_INTENT_STRATEGY``,
        replacing the historical hardcoded 0.5/0.3/0.2 default."""
        self._default_strategy = _DEFAULT_STRATEGY
        if config is not None:
            try:
                self._default_strategy = (
                    float(config.vector_weight),
                    float(config.lexical_weight),
                    float(config.graph_weight),
                    int(getattr(config, "rerank_top_k", 10)),
                )
            except (AttributeError, TypeError, ValueError):
                pass
        self._cache: dict[tuple[str, str], tuple[float, RetrievalResult]] = {}

    def plan(
        self,
        intent: IntentType,
        confidence: float,
        text: str,
        *,
        namespace: str | None = None,
    ) -> RetrievalQuery:
        """Build a :class:`RetrievalQuery` tuned to *intent*.

        Low-confidence classifications (``confidence < 0.3``) fall back to the
        balanced default strategy rather than committing to a skewed one on a
        guess — the classifier itself flags uncertainty via ``confidence``,
        and a wrong skew (e.g. all-graph on a misclassified DEPENDENCY_ANALYSIS
        for what was really a QUESTION) costs more recall than the balanced
        default ever would.
        """
        if confidence < 0.3:
            vector_w, lexical_w, graph_w, top_k = self._default_strategy
        else:
            vector_w, lexical_w, graph_w, top_k = _INTENT_STRATEGY.get(
                intent, self._default_strategy
            )
        return RetrievalQuery(
            text=text,
            top_k=top_k,
            namespace=namespace,
            vector_weight=vector_w,
            lexical_weight=lexical_w,
            graph_weight=graph_w,
            intent=intent.value,
        )

    def _cache_key(self, query: RetrievalQuery) -> tuple[str, str]:
        # Includes intent (not just weights) because the reranker conditions
        # on query.intent too — two queries with identical weights but
        # different intent must not be treated as the same cache entry.
        weights_sig = (
            f"{query.vector_weight:.2f}:{query.lexical_weight:.2f}:{query.graph_weight:.2f}:"
            f"{query.top_k}:{query.namespace or ''}:{query.intent or ''}"
        )
        return (query.text.strip().lower(), weights_sig)

    def get_cached(self, query: RetrievalQuery) -> RetrievalResult | None:
        """Return a cached result for an identical query issued within the TTL, or None."""
        key = self._cache_key(query)
        entry = self._cache.get(key)
        if entry is None:
            return None
        cached_at, result = entry
        if time.time() - cached_at > _CACHE_TTL_SECONDS:
            del self._cache[key]
            return None
        return result

    def put_cached(self, query: RetrievalQuery, result: RetrievalResult) -> None:
        """Store *result* for *query*, evicting the oldest entry if the cache is full."""
        key = self._cache_key(query)
        if len(self._cache) >= _CACHE_MAX_ENTRIES and key not in self._cache:
            oldest_key = min(self._cache, key=lambda k: self._cache[k][0])
            del self._cache[oldest_key]
        self._cache[key] = (time.time(), result)

    async def plan_and_retrieve(
        self,
        retriever: HybridRetriever,
        intent: IntentType,
        confidence: float,
        text: str,
        *,
        namespace: str | None = None,
    ) -> RetrievalResult:
        """Plan a query, serve it from cache if possible, else retrieve and cache it."""
        query = self.plan(intent, confidence, text, namespace=namespace)
        cached = self.get_cached(query)
        if cached is not None:
            return cached
        result = await retriever.retrieve(query)
        self.put_cached(query, result)
        return result
