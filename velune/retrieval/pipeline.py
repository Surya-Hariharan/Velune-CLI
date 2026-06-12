"""Dual-path retrieval pipeline with fast and slow paths."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from velune.context.budget import ContextBudget

logger = logging.getLogger("velune.retrieval.pipeline")


@dataclass
class TaskProfile:
    """Task profile for retrieval decisions."""
    task_type: str  # CODING, REASONING, SUMMARIZATION, etc.
    complexity: str  # LOW, MEDIUM, HIGH
    requires_long_context: bool = False
    latency_sensitive: bool = False


@dataclass
class ContextChunk:
    """A chunk of retrieved context."""
    id: str
    source: str  # "semantic", "symbol", "episodic", "lineage"
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    relevance_score: float = 0.0
    recency_score: float = 0.0
    trust_score: float = 0.0
    combined_score: float = 0.0


@dataclass
class RetrievedContext:
    """Results from retrieval pipeline."""
    chunks: list[ContextChunk]
    total_tokens: int
    fast_path_tokens: int
    slow_path_tokens: int
    retrieval_time_ms: float
    fast_path_time_ms: float
    slow_path_time_ms: float
    cache_hit: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class RetrievalPipeline:
    """Orchestrates dual-path retrieval (fast + slow based on complexity)."""

    # Complexity thresholds
    FAST_PATH_ONLY = {"LOW", "MEDIUM"}
    FAST_AND_SLOW = {"HIGH"}

    def __init__(
        self,
        fast_path_retriever: Any,
        slow_path_retriever: Any,
        reranker: Any,
        cache: Any,
        max_total_chunks: int = 20,
    ) -> None:
        """Initialize pipeline.

        Parameters
        ----------
        fast_path_retriever:
            FastPathRetriever instance
        slow_path_retriever:
            SlowPathRetriever instance
        reranker:
            CrossEncoderReranker instance
        cache:
            RetrievalCache instance
        max_total_chunks:
            Maximum chunks to return
        """
        self.fast_path = fast_path_retriever
        self.slow_path = slow_path_retriever
        self.reranker = reranker
        self.cache = cache
        self.max_total_chunks = max_total_chunks

    async def retrieve(
        self,
        query: str,
        budget: ContextBudget,
        task_profile: TaskProfile,
        workspace_root: str = "",
    ) -> RetrievedContext:
        """Retrieve context using dual-path strategy.

        Algorithm:
        1. Check cache (< 1ms)
        2. Run fast path (< 200ms)
        3. If HIGH complexity: run slow path (< 2000ms)
        4. Merge and deduplicate results
        5. Rerank by combined score
        6. Fit to budget

        Parameters
        ----------
        query:
            Retrieval query
        budget:
            Token budget for retrieval
        task_profile:
            Task complexity and profile
        workspace_root:
            Workspace root for scoping

        Returns
        -------
        RetrievedContext:
            Retrieved chunks with metadata
        """
        start_time = time.perf_counter()

        # Check cache first
        cache_key = self._make_cache_key(query, task_profile)
        cached = await self.cache.get(cache_key)
        if cached:
            logger.debug("Retrieval cache hit")
            cached.cache_hit = True
            cached.metadata["retrieval_time_ms"] = (time.perf_counter() - start_time) * 1000
            return cached

        fast_start = time.perf_counter()
        slow_start = None
        slow_duration = 0.0

        # Always run fast path
        logger.debug(f"Running fast path for query: {query[:50]}...")
        fast_results = await self.fast_path.retrieve(
            query=query,
            budget=budget,
            workspace_root=workspace_root,
        )
        fast_duration = (time.perf_counter() - fast_start) * 1000

        # Conditionally run slow path
        slow_results = []
        if task_profile.complexity in self.FAST_AND_SLOW:
            logger.debug(f"Running slow path (complexity={task_profile.complexity})")
            slow_start = time.perf_counter()

            # Start slow path in background, but wait for it
            try:
                slow_results = await asyncio.wait_for(
                    self.slow_path.retrieve(
                        query=query,
                        fast_results=fast_results,
                        budget=budget,
                        workspace_root=workspace_root,
                    ),
                    timeout=2.0,
                )
            except asyncio.TimeoutError:
                logger.warning("Slow path timed out (>2000ms)")
                slow_results = []

            slow_duration = (time.perf_counter() - slow_start) * 1000

        # Merge results (deduplicate by source ID)
        merged = self._merge_results(fast_results, slow_results)

        # Rerank merged results
        reranked = self.reranker.rerank(merged, query)

        # Fit to budget
        fitted = self._fit_to_budget(reranked, budget)

        total_duration = (time.perf_counter() - start_time) * 1000

        result = RetrievedContext(
            chunks=fitted,
            total_tokens=sum(c.metadata.get("tokens", 100) for c in fitted),
            fast_path_tokens=sum(c.metadata.get("tokens", 100) for c in fast_results),
            slow_path_tokens=sum(c.metadata.get("tokens", 100) for c in slow_results),
            retrieval_time_ms=total_duration,
            fast_path_time_ms=fast_duration,
            slow_path_time_ms=slow_duration,
            cache_hit=False,
            metadata={
                "query_length": len(query),
                "task_complexity": task_profile.complexity,
                "chunks_fast": len(fast_results),
                "chunks_slow": len(slow_results),
                "chunks_final": len(fitted),
                "slow_path_ran": task_profile.complexity in self.FAST_AND_SLOW,
            },
        )

        # Cache result
        await self.cache.set(cache_key, result)

        logger.info(
            f"Retrieval complete: {len(fast_results)} fast + {len(slow_results)} slow "
            f"→ {len(fitted)} final chunks ({total_duration:.0f}ms)"
        )

        return result

    def _make_cache_key(self, query: str, task_profile: TaskProfile) -> str:
        """Generate cache key from query and profile.

        Parameters
        ----------
        query:
            Query text
        task_profile:
            Task profile

        Returns
        -------
        str:
            Cache key
        """
        import hashlib

        key_parts = [
            query.lower().strip(),
            task_profile.complexity,
            task_profile.task_type,
        ]
        key_str = "|".join(key_parts)
        return hashlib.sha256(key_str.encode()).hexdigest()

    def _merge_results(
        self,
        fast_results: list[ContextChunk],
        slow_results: list[ContextChunk],
    ) -> list[ContextChunk]:
        """Merge results from both paths, deduplicating by source ID.

        Parameters
        ----------
        fast_results:
            Results from fast path
        slow_results:
            Results from slow path

        Returns
        -------
        list[ContextChunk]:
            Merged and deduplicated results
        """
        merged: dict[str, ContextChunk] = {}

        # Add fast results first (they're more reliable due to lower latency)
        for chunk in fast_results:
            merged[chunk.id] = chunk

        # Add slow results (updating if better score)
        for chunk in slow_results:
            if chunk.id not in merged:
                merged[chunk.id] = chunk
            else:
                # Keep the one with higher combined score
                if chunk.combined_score > merged[chunk.id].combined_score:
                    merged[chunk.id] = chunk

        return list(merged.values())

    def _fit_to_budget(
        self,
        chunks: list[ContextChunk],
        budget: ContextBudget,
    ) -> list[ContextChunk]:
        """Fit chunks to token budget.

        Parameters
        ----------
        chunks:
            Ranked chunks
        budget:
            Token budget

        Returns
        -------
        list[ContextChunk]:
            Chunks that fit in budget, sorted by relevance
        """
        selected: list[ContextChunk] = []
        total_tokens = 0

        for chunk in chunks[:self.max_total_chunks]:
            chunk_tokens = chunk.metadata.get("tokens", 100)

            if total_tokens + chunk_tokens > budget.retrieval_allocation:
                break

            selected.append(chunk)
            total_tokens += chunk_tokens

        return selected
