"""Tests for dual-path retrieval pipeline."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from velune.retrieval.cache import RetrievalCache
from velune.retrieval.pipeline import (
    ContextBudget,
    ContextChunk,
    RetrievalPipeline,
    TaskProfile,
)
from velune.retrieval.reranker import CrossEncoderReranker


@pytest.fixture
def mock_fast_path():
    """Mock fast path retriever."""
    retriever = AsyncMock()

    async def fast_retrieve(query, budget, workspace_root=""):
        return [
            ContextChunk(
                id="fast_1",
                source="semantic",
                content="Fast result 1",
                metadata={"tokens": 100},
                relevance_score=0.9,
            ),
            ContextChunk(
                id="fast_2",
                source="symbol",
                content="Fast result 2",
                metadata={"tokens": 50},
                relevance_score=0.7,
            ),
        ]

    retriever.retrieve = fast_retrieve
    return retriever


@pytest.fixture
def mock_slow_path():
    """Mock slow path retriever."""
    retriever = AsyncMock()

    async def slow_retrieve(query, fast_results, budget, workspace_root=""):
        return [
            ContextChunk(
                id="slow_1",
                source="lineage",
                content="Slow result 1",
                metadata={"tokens": 75},
                relevance_score=0.6,
            ),
        ]

    retriever.retrieve = slow_retrieve
    return retriever


@pytest.fixture
def pipeline(mock_fast_path, mock_slow_path):
    """Create retrieval pipeline."""
    reranker = CrossEncoderReranker()
    cache = RetrievalCache()

    return RetrievalPipeline(
        fast_path_retriever=mock_fast_path,
        slow_path_retriever=mock_slow_path,
        reranker=reranker,
        cache=cache,
    )


@pytest.fixture
def budget():
    """Create context budget."""
    return ContextBudget(
        total_tokens=100000,
        retrieval_allocation=10000,
        working_memory_allocation=20000,
        output_reservation=1000,
    )


@pytest.mark.asyncio
async def test_fast_path_only_low_complexity(pipeline, budget):
    """Test: LOW complexity triggers fast path only."""
    task = TaskProfile(task_type="GENERAL", complexity="LOW")

    result = await pipeline.retrieve(
        query="test query",
        budget=budget,
        task_profile=task,
    )

    assert len(result.chunks) >= 2
    assert result.fast_path_time_ms > 0
    assert result.slow_path_time_ms == 0.0
    assert result.metadata["slow_path_ran"] is False


@pytest.mark.asyncio
async def test_fast_path_only_medium_complexity(pipeline, budget):
    """Test: MEDIUM complexity triggers fast path only."""
    task = TaskProfile(task_type="CODING", complexity="MEDIUM")

    result = await pipeline.retrieve(
        query="test query",
        budget=budget,
        task_profile=task,
    )

    assert result.fast_path_time_ms > 0
    assert result.slow_path_time_ms == 0.0
    assert result.metadata["slow_path_ran"] is False


@pytest.mark.asyncio
async def test_dual_path_high_complexity(pipeline, budget):
    """Test: HIGH complexity triggers both fast and slow paths."""
    task = TaskProfile(task_type="REASONING", complexity="HIGH")

    result = await pipeline.retrieve(
        query="test query",
        budget=budget,
        task_profile=task,
    )

    assert result.fast_path_time_ms > 0
    assert result.slow_path_time_ms > 0
    assert result.metadata["slow_path_ran"] is True
    assert len(result.chunks) >= 2


@pytest.mark.asyncio
async def test_cache_hit(pipeline, budget):
    """Test: Cache hits return results quickly."""
    task = TaskProfile(task_type="GENERAL", complexity="LOW")

    result1 = await pipeline.retrieve(
        query="same query",
        budget=budget,
        task_profile=task,
    )

    result2 = await pipeline.retrieve(
        query="same query",
        budget=budget,
        task_profile=task,
    )

    assert result2.cache_hit is True
    assert len(result1.chunks) == len(result2.chunks)


@pytest.mark.asyncio
async def test_reranking(pipeline, budget):
    """Test: Results are reranked by combined score."""
    task = TaskProfile(task_type="GENERAL", complexity="LOW")

    result = await pipeline.retrieve(
        query="test query",
        budget=budget,
        task_profile=task,
    )

    scores = [c.combined_score for c in result.chunks]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_token_budget_fitting(pipeline, budget):
    """Test: Results fit within token budget."""
    import dataclasses

    budget = dataclasses.replace(budget, retrieval_allocation=200)

    task = TaskProfile(task_type="GENERAL", complexity="LOW")

    result = await pipeline.retrieve(
        query="test query",
        budget=budget,
        task_profile=task,
    )

    assert result.total_tokens <= budget.retrieval_allocation


@pytest.mark.asyncio
async def test_cache_lru_eviction():
    """Test: Cache evicts LRU entries when full."""
    cache = RetrievalCache()
    cache.MAX_ENTRIES = 3

    for i in range(4):
        await cache.set(f"key{i}", f"result{i}")

    assert len(cache._cache) == 3
    assert "key0" not in cache._cache


def test_cache_hit_rate():
    """Test: Cache hit rate is calculated correctly."""
    cache = RetrievalCache()

    cache._hits = 7
    cache._misses = 3

    assert cache.hit_rate() == 0.7


def test_reranker_score_weights():
    """Test: Reranker combines scores with correct weights."""
    reranker = CrossEncoderReranker()

    chunk = ContextChunk(
        id="test",
        source="semantic",
        content="test",
        metadata={"timestamp": time.time()},
        relevance_score=1.0,
    )

    score = reranker._calculate_combined_score(chunk)
    assert 0.5 <= score <= 1.0


def test_cache_stats():
    """Test: Cache statistics are reported correctly."""
    cache = RetrievalCache()

    cache._hits = 10
    cache._misses = 40
    cache._cache["key1"] = ("result", time.time())

    stats = cache.stats()

    assert stats["hits"] == 10
    assert stats["misses"] == 40
    assert stats["total_requests"] == 50
    assert stats["hit_rate"] == 0.2
    assert stats["entries"] == 1
