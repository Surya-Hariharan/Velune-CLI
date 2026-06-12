"""Unit tests for ContextOrchestrationEngine."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from velune.cli.modes import SessionMode
from velune.context.budget import ContextBudget
from velune.context.sections import ContextChunk, ContextSection
from velune.core.types.model import ModelDescriptor
from velune.orchestration.engine import ContextOrchestrationEngine


def test_engine_create_budget() -> None:
    engine = ContextOrchestrationEngine()
    model = ModelDescriptor(
        model_id="gpt-4",
        provider_id="openai",
        display_name="GPT-4",
        context_length=8192,
        capabilities={},
    )
    budget = engine.create_budget(SessionMode.NORMAL, model)
    assert isinstance(budget, ContextBudget)
    assert budget.total_tokens == 8192


def test_engine_assemble_context() -> None:
    engine = ContextOrchestrationEngine()
    model = ModelDescriptor(
        model_id="gpt-4",
        provider_id="openai",
        display_name="GPT-4",
        context_length=8192,
        capabilities={},
    )
    budget = engine.create_budget(SessionMode.NORMAL, model)
    chunks = [
        ContextChunk(
            section=ContextSection.SYSTEM_PROMPT,
            content="System instruction",
            token_count=10,
            source="system",
        )
    ]
    context, tokens = engine.assemble_context(chunks, budget, model)
    assert "System instruction" in context
    assert tokens > 0


@pytest.mark.asyncio
async def test_orchestrate_context_retrieval() -> None:
    mock_pipeline = MagicMock()
    mock_retrieved = MagicMock()
    mock_chunk = MagicMock()
    mock_chunk.id = "chunk-1"
    mock_chunk.content = "retrieved content"
    mock_chunk.source = "semantic"
    mock_chunk.relevance_score = 0.9
    mock_chunk.recency_score = 0.8
    mock_chunk.combined_score = 0.85
    mock_retrieved.chunks = [mock_chunk]

    mock_pipeline.retrieve = AsyncMock(return_value=mock_retrieved)
    engine = ContextOrchestrationEngine(retrieval_pipeline=mock_pipeline)

    model = ModelDescriptor(
        model_id="gpt-4",
        provider_id="openai",
        display_name="GPT-4",
        context_length=8192,
        capabilities={},
    )
    chunks, budget = await engine.orchestrate_context_retrieval(
        query="test",
        mode=SessionMode.NORMAL,
        model=model,
    )

    assert len(chunks) == 1
    assert chunks[0].content == "retrieved content"
    assert chunks[0].section == ContextSection.RETRIEVED_CONTEXT
    assert isinstance(budget, ContextBudget)


def test_convert_retrieval_results_mapping() -> None:
    engine = ContextOrchestrationEngine()
    budget = ContextBudget(
        total_tokens=1000,
        retrieval_allocation=500,
        working_memory_allocation=300,
        output_reservation=100,
    )

    # Test mapping of different retrieval sources to context sections
    sources = [
        ("semantic", ContextSection.RETRIEVED_CONTEXT),
        ("vector", ContextSection.RETRIEVED_CONTEXT),
        ("symbol", ContextSection.REPOSITORY_SNAPSHOT),
        ("episodic", ContextSection.WORKING_MEMORY),
        ("memory", ContextSection.WORKING_MEMORY),
        ("lineage", ContextSection.COGNITIVE_CONTINUITY),
        ("other", ContextSection.RETRIEVED_CONTEXT),
    ]

    for source_name, expected_section in sources:
        mock_retrieved = MagicMock()
        mock_chunk = MagicMock()
        mock_chunk.id = "chunk-id"
        mock_chunk.content = "some content"
        mock_chunk.source = source_name
        mock_chunk.relevance_score = 0.9
        mock_chunk.recency_score = 0.8
        mock_chunk.combined_score = 0.85
        mock_retrieved.chunks = [mock_chunk]

        converted = engine._convert_retrieval_results(mock_retrieved, budget)
        assert len(converted) == 1
        assert converted[0].section == expected_section


@pytest.mark.asyncio
async def test_orchestrate_context_retrieval_no_pipeline() -> None:
    engine = ContextOrchestrationEngine(retrieval_pipeline=None)
    model = ModelDescriptor(
        model_id="gpt-4",
        provider_id="openai",
        display_name="GPT-4",
        context_length=8192,
        capabilities={},
    )
    chunks, budget = await engine.orchestrate_context_retrieval(
        query="test",
        mode=SessionMode.NORMAL,
        model=model,
    )
    assert chunks == []
    assert budget.total_tokens == 8192


@pytest.mark.asyncio
async def test_orchestrate_context_retrieval_default_task_profile() -> None:
    mock_pipeline = MagicMock()
    mock_retrieved = MagicMock()
    mock_retrieved.chunks = []
    mock_pipeline.retrieve = AsyncMock(return_value=mock_retrieved)

    engine = ContextOrchestrationEngine(retrieval_pipeline=mock_pipeline)
    model = ModelDescriptor(
        model_id="gpt-4",
        provider_id="openai",
        display_name="GPT-4",
        context_length=8192,
        capabilities={},
    )
    # Trigger task_profile is None
    chunks, budget = await engine.orchestrate_context_retrieval(
        query="test",
        mode=SessionMode.NORMAL,
        model=model,
        task_profile=None,
    )
    assert chunks == []
    mock_pipeline.retrieve.assert_called_once()


def test_estimate_chunk_tokens_edge_cases() -> None:
    assert ContextOrchestrationEngine._estimate_chunk_tokens("") == 0
    assert ContextOrchestrationEngine._estimate_chunk_tokens(None) == 0  # type: ignore
    assert ContextOrchestrationEngine._estimate_chunk_tokens("hello") == 1


def test_assemble_context_budget_exceeded() -> None:
    engine = ContextOrchestrationEngine()
    model = ModelDescriptor(
        model_id="gpt-4",
        provider_id="openai",
        display_name="GPT-4",
        context_length=1000,
        capabilities={},
    )
    budget = ContextBudget(
        total_tokens=1000,
        retrieval_allocation=100,
        working_memory_allocation=100,
        output_reservation=100,
    )
    # Large chunk that will exceed budget
    chunks = [
        ContextChunk(
            section=ContextSection.RETRIEVED_CONTEXT,
            content="very long retrieved context " * 200,
            token_count=1500,
            source="semantic",
        )
    ]
    # This shouldn't raise exception, but should log warning/error and return
    ctx, tokens = engine.assemble_context(chunks, budget, model)
    assert tokens >= 0
