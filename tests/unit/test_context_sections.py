"""Tests for ContextSection and ContextChunk."""

import pytest

from velune.context.sections import (
    ContextAssemblyReport,
    ContextChunk,
    ContextSection,
)


def test_context_section_ordering():
    """Test that ContextSection values provide canonical ordering."""
    sections = list(ContextSection)
    values = [s.value for s in sections]

    # Should be ordered 1-7
    assert values == [1, 2, 3, 4, 5, 6, 7]


def test_context_chunk_creation():
    """Test basic ContextChunk creation."""
    chunk = ContextChunk(
        section=ContextSection.RETRIEVED_CONTEXT,
        content="test content",
        token_count=10,
        source="semantic",
        trust_score=0.95,
        priority=0.8,
    )

    assert chunk.section == ContextSection.RETRIEVED_CONTEXT
    assert chunk.content == "test content"
    assert chunk.token_count == 10
    assert chunk.source == "semantic"
    assert chunk.trust_score == 0.95
    assert chunk.priority == 0.8


def test_context_chunk_validation():
    """Test ContextChunk validation."""
    # Invalid trust_score > 1.0
    with pytest.raises(ValueError, match="trust_score must be 0.0-1.0"):
        ContextChunk(
            section=ContextSection.RETRIEVED_CONTEXT,
            content="test",
            token_count=10,
            source="test",
            trust_score=1.5,
        )

    # Invalid empty content
    with pytest.raises(ValueError, match="content must be a non-empty string"):
        ContextChunk(
            section=ContextSection.RETRIEVED_CONTEXT,
            content="",
            token_count=10,
            source="test",
        )

    # Invalid negative token count
    with pytest.raises(ValueError, match="token_count must be non-negative"):
        ContextChunk(
            section=ContextSection.RETRIEVED_CONTEXT,
            content="test",
            token_count=-1,
            source="test",
        )


def test_chunk_is_trimmable():
    """Test is_trimmable logic for different sections."""
    # Trimmable sections
    trimmable = [
        ContextSection.RETRIEVED_CONTEXT,
        ContextSection.WORKING_MEMORY,
        ContextSection.COGNITIVE_CONTINUITY,
        ContextSection.REPOSITORY_SNAPSHOT,
    ]
    for section in trimmable:
        chunk = ContextChunk(
            section=section,
            content="test",
            token_count=10,
            source="test",
        )
        assert chunk.is_trimmable() is True

    # Never-trim sections
    never_trim = [
        ContextSection.SYSTEM_PROMPT,
        ContextSection.ARCHITECTURAL_DRIFT,
        ContextSection.CURRENT_PROMPT,
    ]
    for section in never_trim:
        chunk = ContextChunk(
            section=section,
            content="test",
            token_count=10,
            source="test",
        )
        assert chunk.is_trimmable() is False


def test_chunk_trim_score():
    """Test trim_score calculation."""
    chunk1 = ContextChunk(
        section=ContextSection.RETRIEVED_CONTEXT,
        content="high trust",
        token_count=10,
        source="semantic",
        trust_score=0.9,
        priority=0.8,
    )

    chunk2 = ContextChunk(
        section=ContextSection.RETRIEVED_CONTEXT,
        content="low trust",
        token_count=10,
        source="semantic",
        trust_score=0.2,
        priority=0.5,
    )

    # chunk1 should have higher trim score (lower trim priority)
    assert chunk1.trim_score() > chunk2.trim_score()


def test_context_assembly_report():
    """Test ContextAssemblyReport."""
    report = ContextAssemblyReport(
        total_chunks_received=10,
        total_tokens_requested=8192,
        total_tokens_assembled=7500,
        sections_present=[ContextSection.RETRIEVED_CONTEXT, ContextSection.WORKING_MEMORY],
        sections_trimmed={ContextSection.RETRIEVED_CONTEXT: 500},
        chunks_dropped=2,
        budget_exceeded=False,
    )

    assert report.total_chunks_received == 10
    assert report.chunks_dropped == 2
    assert not report.budget_exceeded

    report_str = str(report)
    assert "ContextAssemblyReport" in report_str
    assert "7500/8192" in report_str
