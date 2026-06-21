"""Formal context budget allocation system.

Defines token budget constraints for each session mode and allocates
budget across retrieval, working memory, and system overhead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.cli.modes import SessionMode


@dataclass(frozen=True)
class ContextBudget:
    """Token budget allocation across context sections.

    Frozen dataclass enforcing immutability once created. Budget is
    split proportionally across retrieval, working memory, system overhead,
    and output reservation based on the active SessionMode.
    """

    total_tokens: int
    retrieval_allocation: int
    working_memory_allocation: int
    output_reservation: int
    system_allocation: int = 512

    @classmethod
    def from_mode(cls, mode: SessionMode, model_context_window: int) -> ContextBudget:
        """Create a budget for the given session mode and model context.

        Args:
            mode: SessionMode (OPTIMUS, NORMAL, or GODLY)
            model_context_window: Model's reported context length in tokens

        Returns:
            ContextBudget with allocations tuned to the mode's constraints
        """
        from velune.cli.modes import SessionMode

        # Phase 1: Determine total usable tokens based on mode
        if mode == SessionMode.OPTIMUS:
            total = min(4096, model_context_window)
        elif mode == SessionMode.NORMAL:
            total = min(16384, model_context_window)
        elif mode == SessionMode.GODLY:
            total = model_context_window
        else:
            # Fallback to NORMAL
            total = min(16384, model_context_window)

        # Phase 2: Reserve output space and system overhead
        system_alloc = 512
        output_reserve = min(2048, total // 4)
        usable = total - output_reserve - system_alloc

        # Phase 3: Allocate remaining budget proportionally
        # retrieval: 55%, working_memory: 35%
        retrieval_alloc = int(usable * 0.55)
        working_memory_alloc = int(usable * 0.35)

        return cls(
            total_tokens=total,
            retrieval_allocation=retrieval_alloc,
            working_memory_allocation=working_memory_alloc,
            system_allocation=system_alloc,
            output_reservation=output_reserve,
        )

    @property
    def usable_tokens(self) -> int:
        """Total tokens available for context (minus output and system)."""
        return self.total_tokens - self.system_allocation - self.output_reservation

    @property
    def unallocated_tokens(self) -> int:
        """Tokens not explicitly allocated to retrieval or working memory."""
        allocated = (
            self.retrieval_allocation
            + self.working_memory_allocation
            + self.system_allocation
            + self.output_reservation
        )
        return self.total_tokens - allocated

    def __str__(self) -> str:
        """Human-readable budget summary."""
        return (
            f"ContextBudget(total={self.total_tokens}, "
            f"retrieval={self.retrieval_allocation}, "
            f"working_memory={self.working_memory_allocation}, "
            f"system={self.system_allocation}, "
            f"output_reserve={self.output_reservation})"
        )
