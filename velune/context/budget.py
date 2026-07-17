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
    def from_mode(
        cls,
        mode: SessionMode,
        model_context_window: int,
        retrieval_bias: float = 0.55,
    ) -> ContextBudget:
        """Create a budget for the given session mode and model context.

        Args:
            mode: SessionMode (OPTIMUS, NORMAL, or GODLY)
            model_context_window: Model's reported context length in tokens
            retrieval_bias: Fraction of usable tokens given to retrieval vs.
                working memory, clamped to [0.2, 0.8] so neither section is
                ever starved entirely. Defaults to 0.55 — the original fixed
                split — so existing callers are unaffected.

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
        bias = min(0.8, max(0.2, retrieval_bias))
        retrieval_alloc = int(usable * bias)
        working_memory_alloc = int(usable * (1.0 - bias))

        return cls(
            total_tokens=total,
            retrieval_allocation=retrieval_alloc,
            working_memory_allocation=working_memory_alloc,
            system_allocation=system_alloc,
            output_reservation=output_reserve,
        )

    @classmethod
    def for_chat(
        cls,
        mode: SessionMode,
        model_context_window: int,
        retrieval_bias: float = 0.55,
    ) -> ContextBudget:
        """Create a budget for an interactive chat turn.

        Chat differs from council context assembly in one way: the output
        reservation must be generous enough for a full assistant reply (the
        council's 2048-token cap is tuned for structured phase outputs). The
        input side (``usable_tokens``) bounds how much conversation history +
        retrieved context may be sent, so a small local model is never handed
        more than its window and a large model is not artificially capped.

        Args:
            mode: SessionMode (OPTIMUS, NORMAL, or GODLY)
            model_context_window: Model's reported context length in tokens
            retrieval_bias: Fraction of usable tokens given to retrieval vs.
                working memory, clamped to [0.2, 0.8]. Defaults to 0.55 — the
                original fixed split — so existing callers are unaffected.
                Callers can bias this per query intent, e.g. higher for
                intents that need more retrieved material (SEARCH,
                DEPENDENCY_ANALYSIS), lower for intents that lean on
                conversational continuity (DEBUG).

        Returns:
            ContextBudget sized for a chat turn in the given mode.
        """
        from velune.cli.modes import SessionMode

        window = max(1024, int(model_context_window or 8192))
        if mode == SessionMode.OPTIMUS:
            total = min(4096, window)
        elif mode == SessionMode.GODLY:
            total = window
        else:  # NORMAL or unknown
            total = min(16384, window)

        system_alloc = 512
        output_reserve = min(4096, max(256, total // 4))
        usable = total - output_reserve - system_alloc
        if usable < 256:
            # Tiny windows: shrink the reply reservation before starving input.
            output_reserve = max(256, total // 2 - system_alloc)
            usable = max(128, total - output_reserve - system_alloc)

        bias = min(0.8, max(0.2, retrieval_bias))

        return cls(
            total_tokens=total,
            retrieval_allocation=int(usable * bias),
            working_memory_allocation=int(usable * (1.0 - bias)),
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
