"""Integration tests for context budget and assembly system."""

import pytest

from velune.cli.modes import SessionMode
from velune.context.assembler import ContextAssembler
from velune.context.budget import ContextBudget
from velune.context.sections import ContextChunk, ContextSection
from velune.context.token_counter import TokenCounter
from velune.core.types.model import ModelDescriptor


@pytest.fixture
def model():
    """Provide a test model descriptor."""
    return ModelDescriptor(
        model_id="gpt-4-turbo",
        provider_id="openai",
        display_name="GPT-4 Turbo",
        context_length=128000,
        capabilities={},
        family="openai",
    )


def test_full_context_workflow(model):
    """Test complete workflow: budget creation -> chunk assembly -> token counting."""
    # Step 1: Create budget for NORMAL mode
    budget = ContextBudget.from_mode(SessionMode.NORMAL, model.context_length)
    assert budget.total_tokens == 16384

    # Step 2: Create various context chunks
    chunks = [
        # System prompt (never trim)
        ContextChunk(
            section=ContextSection.SYSTEM_PROMPT,
            content="You are a helpful AI assistant specializing in code review and refactoring.",
            token_count=18,
            source="system",
            trust_score=1.0,
            priority=1.0,
        ),
        # Architectural drift (never trim)
        ContextChunk(
            section=ContextSection.ARCHITECTURAL_DRIFT,
            content="CRITICAL: Circular dependency detected in module imports.",
            token_count=12,
            source="architecture",
            trust_score=1.0,
            priority=1.0,
        ),
        # Retrieved context (high trust, can trim)
        ContextChunk(
            section=ContextSection.RETRIEVED_CONTEXT,
            content="Found similar pattern in database migration layer.",
            token_count=12,
            source="semantic",
            trust_score=0.92,
            priority=0.8,
        ),
        # Working memory
        ContextChunk(
            section=ContextSection.WORKING_MEMORY,
            content="User asked about refactoring the authentication module.",
            token_count=12,
            source="conversation",
            trust_score=1.0,
            priority=0.9,
        ),
        # Current prompt (never trim)
        ContextChunk(
            section=ContextSection.CURRENT_PROMPT,
            content="Please suggest improvements to the codebase.",
            token_count=12,
            source="user",
            trust_score=1.0,
            priority=1.0,
        ),
    ]

    # Step 3: Assemble context with budget constraints
    assembler = ContextAssembler()
    assembled_context, report = assembler.assemble(chunks, budget, model)

    # Verify report
    assert report.total_chunks_received == 5
    assert len(report.sections_present) >= 3  # At least system, drift, current
    assert not report.budget_exceeded

    # Step 4: Count tokens in assembled context
    token_count = TokenCounter.count(assembled_context, model)
    assert token_count <= budget.total_tokens

    # Verify critical sections are present
    assert "You are a helpful AI" in assembled_context
    assert "CRITICAL: Circular dependency" in assembled_context
    assert "Please suggest improvements" in assembled_context


def test_budget_allocation_breakdown():
    """Test that budget allocations follow the correct ratios."""
    ModelDescriptor(
        model_id="test",
        provider_id="test",
        display_name="Test",
        context_length=16384,
        capabilities={},
    )

    budget = ContextBudget.from_mode(SessionMode.NORMAL, 16384)

    # Calculate expected values
    output_reserve = min(2048, 16384 // 4)  # 2048
    system = 512
    usable = 16384 - output_reserve - system  # 13824

    expected_retrieval = int(usable * 0.55)  # 7603
    expected_working = int(usable * 0.35)  # 4838

    assert budget.output_reservation == output_reserve
    assert budget.system_allocation == system
    assert budget.retrieval_allocation == expected_retrieval
    assert budget.working_memory_allocation == expected_working


def test_context_trimming_under_pressure():
    """Test context trimming when budget is severely constrained."""
    model = ModelDescriptor(
        model_id="test",
        provider_id="test",
        display_name="Test",
        context_length=4096,
        capabilities={},
    )

    # Create very tight budget (OPTIMUS mode)
    budget = ContextBudget.from_mode(SessionMode.OPTIMUS, 4096)

    # Create many retrieved chunks with varying trust
    chunks = []
    for i in range(10):
        chunks.append(
            ContextChunk(
                section=ContextSection.RETRIEVED_CONTEXT,
                content=f"Retrieved context {i}",
                token_count=200,
                source="semantic",
                trust_score=0.5 + (i * 0.05),  # 0.5 to 0.95
            )
        )

    # Add required sections
    chunks.insert(
        0,
        ContextChunk(
            section=ContextSection.SYSTEM_PROMPT,
            content="System prompt",
            token_count=50,
            source="system",
        ),
    )
    chunks.append(
        ContextChunk(
            section=ContextSection.CURRENT_PROMPT,
            content="User request",
            token_count=50,
            source="user",
        ),
    )

    # Assemble with tight constraints
    assembler = ContextAssembler()
    assembled_context, report = assembler.assemble(chunks, budget, model)

    # Should have dropped low-trust retrieval chunks
    assert report.chunks_dropped > 0
    assert "Retrieved context 0" not in assembled_context  # Lowest trust

    # But system and current should be present
    assert "System prompt" in assembled_context
    assert "User request" in assembled_context


def test_section_separator_format():
    """Test that section separators follow the specified format."""
    model = ModelDescriptor(
        model_id="test",
        provider_id="test",
        display_name="Test",
        context_length=16384,
        capabilities={},
    )

    budget = ContextBudget.from_mode(SessionMode.NORMAL, 16384)

    chunks = [
        ContextChunk(
            section=ContextSection.SYSTEM_PROMPT,
            content="System",
            token_count=10,
            source="system",
        ),
        ContextChunk(
            section=ContextSection.RETRIEVED_CONTEXT,
            content="Retrieved",
            token_count=20,
            source="semantic",
        ),
    ]

    assembler = ContextAssembler()
    assembled_context, _ = assembler.assemble(chunks, budget, model)

    # Check separator format: --- SECTION: NAME (N of 7) ---
    assert "--- SECTION: SYSTEM_PROMPT (1 of 7) ---" in assembled_context
    assert "--- SECTION: RETRIEVED_CONTEXT (5 of 7) ---" in assembled_context
    assert "--- END SECTION ---" in assembled_context


def test_mode_specific_budgets():
    """Test that different modes create appropriate budgets."""
    model = ModelDescriptor(
        model_id="test",
        provider_id="test",
        display_name="Test",
        context_length=200000,
        capabilities={},
    )

    # OPTIMUS: smallest budget
    optimus_budget = ContextBudget.from_mode(SessionMode.OPTIMUS, model.context_length)
    assert optimus_budget.total_tokens == 4096

    # NORMAL: medium budget
    normal_budget = ContextBudget.from_mode(SessionMode.NORMAL, model.context_length)
    assert normal_budget.total_tokens == 16384

    # GODLY: full budget
    godly_budget = ContextBudget.from_mode(SessionMode.GODLY, model.context_length)
    assert godly_budget.total_tokens == 200000

    # Verify OPTIMUS is most constrained
    assert optimus_budget.retrieval_allocation < normal_budget.retrieval_allocation
    assert normal_budget.retrieval_allocation < godly_budget.retrieval_allocation
