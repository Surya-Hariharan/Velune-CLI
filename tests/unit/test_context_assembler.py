"""Tests for ContextAssembler."""

import pytest

from velune.cli.modes import SessionMode
from velune.context.assembler import ContextAssembler
from velune.context.budget import ContextBudget
from velune.context.sections import ContextChunk, ContextSection
from velune.core.types.model import ModelDescriptor


@pytest.fixture
def model():
    """Provide a test model descriptor."""
    return ModelDescriptor(
        model_id="test-model",
        provider_id="test",
        display_name="Test",
        context_length=16384,
        capabilities={},
    )


@pytest.fixture
def budget(model):
    """Provide a test budget."""
    return ContextBudget.from_mode(SessionMode.NORMAL, model.context_length)


def test_assembler_empty_chunks(budget, model):
    """Test assembling with no chunks."""
    assembler = ContextAssembler()
    content, report = assembler.assemble([], budget, model)

    assert content == ""
    assert report.total_chunks_received == 0
    assert report.total_tokens_assembled == 0


def test_assembler_canonical_ordering(budget, model):
    """Test that sections are assembled in canonical order."""
    assembler = ContextAssembler()

    # Create chunks in reverse order
    chunks = [
        ContextChunk(
            section=ContextSection.CURRENT_PROMPT,
            content="User prompt",
            token_count=10,
            source="user",
        ),
        ContextChunk(
            section=ContextSection.RETRIEVED_CONTEXT,
            content="Retrieved",
            token_count=50,
            source="semantic",
        ),
        ContextChunk(
            section=ContextSection.SYSTEM_PROMPT,
            content="System",
            token_count=20,
            source="system",
        ),
    ]

    content, report = assembler.assemble(chunks, budget, model)

    # Check that sections appear in order: SYSTEM, ..., RETRIEVED, ..., CURRENT
    system_pos = content.find("SYSTEM_PROMPT")
    retrieved_pos = content.find("RETRIEVED_CONTEXT")
    current_pos = content.find("CURRENT_PROMPT")

    assert system_pos < retrieved_pos < current_pos


def test_assembler_never_trim_sections(budget, model):
    """Test that never-trim sections are preserved."""
    assembler = ContextAssembler()

    # Create system prompt (never trim)
    system_chunk = ContextChunk(
        section=ContextSection.SYSTEM_PROMPT,
        content="System instructions",
        token_count=100,
        source="system",
    )

    # Create architectural drift (never trim)
    arch_chunk = ContextChunk(
        section=ContextSection.ARCHITECTURAL_DRIFT,
        content="URGENT: Breaking changes",
        token_count=100,
        source="architecture",
    )

    # Create current prompt (never trim)
    current_chunk = ContextChunk(
        section=ContextSection.CURRENT_PROMPT,
        content="User request",
        token_count=100,
        source="user",
    )

    chunks = [system_chunk, arch_chunk, current_chunk]
    content, report = assembler.assemble(chunks, budget, model)

    # All three should be present even if they exceed budget
    assert "System instructions" in content
    assert "URGENT: Breaking changes" in content
    assert "User request" in content


def test_assembler_trim_retrieved_context(budget, model):
    """Test trimming low-trust retrieved context."""
    assembler = ContextAssembler()

    # Create retrieved chunks with varying trust scores
    chunks = [
        ContextChunk(
            section=ContextSection.RETRIEVED_CONTEXT,
            content="High trust content",
            token_count=1000,  # Fits within retrieval allocation
            source="semantic",
            trust_score=0.95,
        ),
        ContextChunk(
            section=ContextSection.RETRIEVED_CONTEXT,
            content="Low trust content",
            token_count=5000,
            source="semantic",
            trust_score=0.2,
        ),
    ]

    # Small retrieval budget forces trimming
    small_budget = ContextBudget.from_mode(SessionMode.OPTIMUS, 4096)

    content, report = assembler.assemble(chunks, small_budget, model)

    # High-trust chunk should be kept
    assert "High trust content" in content
    # Low-trust chunk should be dropped
    assert "Low trust content" not in content


def test_assembler_trim_working_memory(budget, model):
    """Test trimming oldest turns from working memory."""
    assembler = ContextAssembler()

    # Create working memory chunks representing turns
    chunks = [
        ContextChunk(
            section=ContextSection.WORKING_MEMORY,
            content="Turn 1",
            token_count=3000,
            source="conversation",
            metadata={"turn": 1},
        ),
        ContextChunk(
            section=ContextSection.WORKING_MEMORY,
            content="Turn 2",
            token_count=3000,
            source="conversation",
            metadata={"turn": 2},
        ),
        ContextChunk(
            section=ContextSection.WORKING_MEMORY,
            content="Turn 3",
            token_count=500,  # Fits within working memory allocation
            source="conversation",
            metadata={"turn": 3},
        ),
    ]

    # Small working memory budget forces trimming
    small_budget = ContextBudget.from_mode(SessionMode.OPTIMUS, 4096)

    content, report = assembler.assemble(chunks, small_budget, model)

    # Most recent turn should be kept
    assert "Turn 3" in content
    # Oldest turn should be dropped
    assert "Turn 1" not in content


def test_assembler_section_separators(budget, model):
    """Test that section separators are properly formatted."""
    assembler = ContextAssembler()

    chunks = [
        ContextChunk(
            section=ContextSection.SYSTEM_PROMPT,
            content="System",
            token_count=10,
            source="system",
        ),
    ]

    content, report = assembler.assemble(chunks, budget, model)

    # Check for proper separators
    assert "--- SECTION: SYSTEM_PROMPT" in content
    assert "--- END SECTION ---" in content


def test_assembler_report_accuracy(budget, model):
    """Test that assembly report accurately describes the result."""
    assembler = ContextAssembler()

    chunks = [
        ContextChunk(
            section=ContextSection.SYSTEM_PROMPT,
            content="System",
            token_count=10,
            source="system",
        ),
        ContextChunk(
            section=ContextSection.RETRIEVED_CONTEXT,
            content="Content",
            token_count=50,
            source="semantic",
        ),
    ]

    content, report = assembler.assemble(chunks, budget, model)

    assert report.total_chunks_received == 2
    assert len(report.sections_present) == 2
    assert ContextSection.SYSTEM_PROMPT in report.sections_present
    assert ContextSection.RETRIEVED_CONTEXT in report.sections_present
