"""Context window manager (token budget)."""

from typing import Optional
from velune.core.types import ContextWindow, ContextChunk, ContextPriority
from velune.context.window.budget import TokenBudget
from velune.context.window.assembler import ContextAssembler


class ContextWindowManager:
    """Manages the context window with token budgeting."""

    def __init__(self, max_tokens: int = 128000):
        self.max_tokens = max_tokens
        self.budget = TokenBudget(max_tokens)
        self.assembler = ContextAssembler()

    def allocate_budget(self) -> dict[str, int]:
        """Allocate token budget across priority tiers."""
        return self.budget.allocate()

    def assemble_context(
        self,
        chunks: list[ContextChunk],
        budget: Optional[dict[str, int]] = None,
    ) -> ContextWindow:
        """Assemble context window from ranked chunks."""
        if budget is None:
            budget = self.allocate_budget()
        
        return self.assembler.assemble(chunks, budget, self.max_tokens)

    def estimate_tokens(self, text: str) -> int:
        """Estimate token count for text."""
        # Rough estimate: 1 token ≈ 4 characters
        return len(text) // 4

    def get_utilization(self, context_window: ContextWindow) -> float:
        """Get current context window utilization."""
        return context_window.utilization
