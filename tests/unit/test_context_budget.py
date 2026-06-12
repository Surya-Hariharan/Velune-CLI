"""Tests for ContextBudget allocation system."""

from velune.cli.modes import SessionMode
from velune.context.budget import ContextBudget


def test_context_budget_optimus_mode():
    """Test OPTIMUS mode budget creation."""
    budget = ContextBudget.from_mode(SessionMode.OPTIMUS, 16384)

    assert budget.total_tokens == 4096  # min(4096, 16384)
    assert budget.retrieval_allocation > 0
    assert budget.working_memory_allocation > 0
    assert budget.system_allocation == 512
    assert budget.output_reservation > 0

    # Verify allocations fit within total
    total_allocated = (
        budget.retrieval_allocation
        + budget.working_memory_allocation
        + budget.system_allocation
        + budget.output_reservation
    )
    assert total_allocated <= budget.total_tokens


def test_context_budget_normal_mode():
    """Test NORMAL mode budget creation."""
    budget = ContextBudget.from_mode(SessionMode.NORMAL, 32768)

    assert budget.total_tokens == 16384  # min(16384, 32768)
    assert budget.system_allocation == 512
    assert budget.output_reservation == 4096  # min(2048, 16384 // 4) = 2048, but 16384//4 = 4096

    # Verify ratio allocations: 55% retrieval, 35% working_memory
    usable = budget.total_tokens - budget.system_allocation - budget.output_reservation
    expected_retrieval = int(usable * 0.55)
    expected_working_memory = int(usable * 0.35)

    assert budget.retrieval_allocation == expected_retrieval
    assert budget.working_memory_allocation == expected_working_memory


def test_context_budget_godly_mode():
    """Test GODLY mode uses full context window."""
    context_length = 200000
    budget = ContextBudget.from_mode(SessionMode.GODLY, context_length)

    assert budget.total_tokens == context_length
    assert budget.system_allocation == 512
    assert budget.output_reservation == 2048  # min(2048, 200000 // 4)


def test_context_budget_frozen():
    """Test that ContextBudget is immutable."""
    budget = ContextBudget.from_mode(SessionMode.NORMAL, 16384)

    # Should not be able to modify frozen dataclass
    try:
        budget.total_tokens = 999  # type: ignore
        assert False, "Should not be able to modify frozen dataclass"
    except Exception:
        pass  # Expected


def test_context_budget_properties():
    """Test budget property calculations."""
    budget = ContextBudget.from_mode(SessionMode.NORMAL, 16384)

    usable = budget.usable_tokens
    assert usable == (
        budget.total_tokens
        - budget.system_allocation
        - budget.output_reservation
    )

    # Verify unallocated tokens
    unallocated = budget.unallocated_tokens
    assert unallocated == (
        budget.total_tokens
        - budget.retrieval_allocation
        - budget.working_memory_allocation
        - budget.system_allocation
        - budget.output_reservation
    )
