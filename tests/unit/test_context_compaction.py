"""Tests for context compaction pipeline."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from velune.memory.compaction import ContextCompactor, HierarchicalSummaryGenerator
from velune.memory.tiers.working import MemoryTurn, WorkingMemoryTier


@pytest.fixture
def working_tier():
    """Create a working memory tier for testing."""
    return WorkingMemoryTier(session_id="test-session")


@pytest.fixture
def mock_provider():
    """Create a mock LLM provider."""
    provider = MagicMock()
    provider.default_model = "test-model"

    async def mock_infer(request):
        response = MagicMock()
        response.content = "• Key decision: Use async/await\n• Bug fixed: Connection timeout\n• Files affected: auth.py, utils.py"
        return response

    provider.infer = AsyncMock(side_effect=mock_infer)
    return provider


@pytest.fixture
def mock_episodic_memory():
    """Create a mock episodic memory."""
    memory = MagicMock()
    memory.record_turn = AsyncMock(return_value="turn-123")
    return memory


@pytest.fixture
def compactor(mock_provider, working_tier, mock_episodic_memory):
    """Create a context compactor for testing."""
    return ContextCompactor(
        provider=mock_provider,
        working_tier=working_tier,
        episodic_memory=mock_episodic_memory,
        max_context_tokens=100000,
    )


@pytest.mark.asyncio
async def test_compaction_trigger_by_turn_count(compactor):
    """Test that compaction triggers when turn count exceeds threshold."""
    # Should not trigger with 30 turns
    should_compact = await compactor.should_compact(turn_count=30, current_token_count=10000)
    assert not should_compact

    # Should trigger with 31 turns
    should_compact = await compactor.should_compact(turn_count=31, current_token_count=10000)
    assert should_compact


@pytest.mark.asyncio
async def test_compaction_trigger_by_context_utilization(compactor):
    """Test that compaction triggers when context utilization exceeds 75%."""
    max_tokens = compactor.max_context_tokens  # 100000

    # Should not trigger at 75% utilization
    should_compact = await compactor.should_compact(
        turn_count=15,
        current_token_count=int(max_tokens * 0.75),
    )
    assert not should_compact

    # Should trigger at 76% utilization
    should_compact = await compactor.should_compact(
        turn_count=15,
        current_token_count=int(max_tokens * 0.76),
    )
    assert should_compact


@pytest.mark.asyncio
async def test_compaction_trigger_on_session_end(compactor):
    """Test that compaction always triggers at session end (if > 10 turns)."""
    # Should not trigger with <= 10 turns even at session end
    should_compact = await compactor.should_compact(
        turn_count=10,
        current_token_count=5000,
        session_end=True,
    )
    assert not should_compact

    # Should trigger with > 10 turns at session end
    should_compact = await compactor.should_compact(
        turn_count=11,
        current_token_count=5000,
        session_end=True,
    )
    assert should_compact


@pytest.mark.asyncio
async def test_compaction_with_35_turns(compactor, working_tier):
    """Test: 35 turns triggers compaction (keeps last 10, compacts 25)."""
    # Add 35 turns to working memory
    for i in range(35):
        working_tier.add_turn("user" if i % 2 == 0 else "assistant", f"Turn {i} content")

    # Verify we have 35 turns
    assert len(working_tier.get_turns()) == 35

    # Perform compaction
    stats = await compactor.compact(session_id="test-session")

    # Verify compaction occurred
    assert stats is not None
    assert stats.turns_compacted == 25
    assert stats.original_turn_count == 25

    # Verify working memory now has <= 15 entries (10 recent + 1 summary)
    remaining_turns = len(working_tier.get_turns())
    assert remaining_turns <= 15


@pytest.mark.asyncio
async def test_quality_guard_rejects_empty_summary(compactor):
    """Test: Quality guard rejects empty summary."""
    turns = [MemoryTurn(role="user", content="content " * 100)]

    # Mock provider to return empty summary
    compactor.provider.infer = AsyncMock()
    compactor.provider.infer.return_value = MagicMock(content="")

    # Attempt compaction
    stats = await compactor.compact(session_id="test-session", turns_to_summarize=turns)

    # Should fail validation
    assert stats is None


@pytest.mark.asyncio
async def test_quality_guard_rejects_refusal(compactor):
    """Test: Quality guard rejects model refusals."""
    turns = [MemoryTurn(role="user", content="content " * 100)]

    # Mock provider to return refusal
    compactor.provider.infer = AsyncMock()
    compactor.provider.infer.return_value = MagicMock(
        content="I don't have access to that information."
    )

    # Attempt compaction
    stats = await compactor.compact(session_id="test-session", turns_to_summarize=turns)

    # Should fail validation
    assert stats is None


@pytest.mark.asyncio
async def test_quality_guard_rejects_oversized_summary(compactor):
    """Test: Quality guard rejects summary > 30% of original size."""
    # Create turns with ~400 chars (~100 tokens)
    turns = [MemoryTurn(role="user", content="x" * 200)]

    # Mock provider to return summary that's too large (~300 chars = ~75 tokens = 75% of original)
    compactor.provider.infer = AsyncMock()
    compactor.provider.infer.return_value = MagicMock(content="y" * 300)

    # Attempt compaction
    stats = await compactor.compact(session_id="test-session", turns_to_summarize=turns)

    # Should fail validation (too large)
    assert stats is None


@pytest.mark.asyncio
async def test_compacted_summary_stored_in_episodic(compactor, mock_episodic_memory, working_tier):
    """Test: Compacted summary is stored in episodic memory with correct tag."""
    # Add turns to working memory
    for i in range(35):
        working_tier.add_turn("user" if i % 2 == 0 else "assistant", f"Turn {i} content")

    # Perform compaction
    stats = await compactor.compact(session_id="test-session")

    # Verify episodic memory was called
    assert mock_episodic_memory.record_turn.called

    # Check the call arguments
    call_args = mock_episodic_memory.record_turn.call_args
    assert call_args is not None
    assert call_args.kwargs["role"] == "system"
    assert call_args.kwargs["metadata"]["tag"] == "compaction_summary"
    assert call_args.kwargs["metadata"]["original_turn_count"] == 25


@pytest.mark.asyncio
async def test_working_memory_after_compaction(compactor, working_tier):
    """Test: Working memory after compaction has <= 15 entries (10 recent + 1 summary)."""
    # Add 35 turns
    for i in range(35):
        working_tier.add_turn("user" if i % 2 == 0 else "assistant", f"Turn {i} content")

    initial_count = len(working_tier.get_turns())
    assert initial_count == 35

    # Perform compaction
    stats = await compactor.compact(session_id="test-session")

    # Verify working memory is compacted
    final_count = len(working_tier.get_turns())
    assert final_count <= 15
    assert final_count == stats.turns_kept


@pytest.mark.asyncio
async def test_compression_ratio_calculation(compactor, working_tier):
    """Test: Compression ratio is calculated correctly."""
    # Add turns with known size
    for i in range(35):
        working_tier.add_turn("user", "x" * 100)  # ~25 chars per turn

    # Perform compaction
    stats = await compactor.compact(session_id="test-session")

    # Verify compression ratio
    assert stats is not None
    assert stats.compression_ratio > 1.0
    assert stats.original_token_count > stats.summary_token_count


@pytest.mark.asyncio
async def test_hierarchical_summary_generation():
    """Test: Session-level summary generated from compaction summaries."""
    provider = AsyncMock()
    provider.default_model = "test-model"
    provider.infer = AsyncMock()
    provider.infer.return_value = MagicMock(
        content="Session focused on authentication refactoring and bug fixes."
    )

    episodic_memory = AsyncMock()

    generator = HierarchicalSummaryGenerator(provider, episodic_memory)

    # Generate session summary from multiple compaction summaries
    compaction_summaries = [
        "• Fixed login bug\n• Refactored auth module",
        "• Added OAuth2 support\n• Updated documentation",
    ]

    summary = await generator.generate_session_summary(
        session_id="test-session",
        compaction_summaries=compaction_summaries,
    )

    # Verify summary was generated
    assert summary is not None
    assert "authentication" in summary.lower() or "auth" in summary.lower()

    # Verify provider was called
    assert provider.infer.called


@pytest.mark.asyncio
async def test_empty_summaries_list():
    """Test: Empty compaction summaries list returns None."""
    provider = AsyncMock()
    episodic_memory = AsyncMock()

    generator = HierarchicalSummaryGenerator(provider, episodic_memory)

    summary = await generator.generate_session_summary(
        session_id="test-session",
        compaction_summaries=[],
    )

    # Should return None for empty list
    assert summary is None


@pytest.mark.asyncio
async def test_token_estimation():
    """Test: Token estimation is reasonable."""
    compactor_inst = ContextCompactor(
        provider=MagicMock(),
        working_tier=WorkingMemoryTier(),
        episodic_memory=AsyncMock(),
    )

    # 1000 characters should estimate to ~250 tokens (1 token per 4 chars)
    text = "x" * 1000
    estimated_tokens = compactor_inst._estimate_tokens_string(text)
    assert estimated_tokens == 250

    # Very short text should estimate to at least 1 token
    text = "x"
    estimated_tokens = compactor_inst._estimate_tokens_string(text)
    assert estimated_tokens >= 1


@pytest.mark.asyncio
async def test_compaction_with_mixed_roles(compactor, working_tier):
    """Test: Compaction handles mixed user/assistant/system roles."""
    # Add turns with mixed roles
    for i in range(35):
        role = ["user", "assistant", "system"][i % 3]
        working_tier.add_turn(role, f"Turn {i} content")

    # Perform compaction
    stats = await compactor.compact(session_id="test-session")

    # Should successfully compact
    assert stats is not None
    assert stats.turns_compacted == 25


@pytest.mark.asyncio
async def test_multiple_compaction_cycles(compactor, working_tier):
    """Test: Multiple compaction cycles work correctly."""
    # First cycle: add 35 turns
    for i in range(35):
        working_tier.add_turn("user", f"Batch1-Turn{i}")

    # First compaction
    stats1 = await compactor.compact(session_id="test-session")
    assert stats1 is not None
    remaining_after_first = len(working_tier.get_turns())

    # Add more turns (second batch)
    for i in range(20):
        working_tier.add_turn("assistant", f"Batch2-Turn{i}")

    # Should not trigger compaction (under 30 total)
    total_before_second = len(working_tier.get_turns())
    assert total_before_second < 31

    # Second compaction (forced)
    stats2 = await compactor.compact(session_id="test-session")

    # Both compactions should have succeeded
    assert stats1 is not None
    assert stats2 is not None
