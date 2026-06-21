"""Context Engineering Subsystem.

Manages token budgets, section ordering, and context assembly.
"""

from velune.context.assembler import ContextAssembler
from velune.context.budget import ContextBudget
from velune.context.sections import ContextAssemblyReport, ContextChunk, ContextSection
from velune.context.token_counter import TokenCounter, estimate_tokens
from velune.context.utilization import ContextUtilizationTracker

__all__ = [
    # Budget and allocation
    "ContextBudget",
    # Sections and chunks
    "ContextSection",
    "ContextChunk",
    "ContextAssemblyReport",
    # Assembly and token counting
    "ContextAssembler",
    "TokenCounter",
    "estimate_tokens",
    # Context utilization
    "ContextUtilizationTracker",
]
