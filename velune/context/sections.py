"""Context section definitions and chunk metadata.

Defines the canonical sections that comprise the context window,
with explicit ordering and priority handling for trimming.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class ContextSection(IntEnum):
    """Canonical sections in the context window, ordered by priority.

    Lower numbers = higher priority in the final assembled context.
    Never trimmed: SYSTEM_PROMPT (always first), ARCHITECTURAL_DRIFT (urgent),
    CURRENT_PROMPT (always last).
    """

    SYSTEM_PROMPT = 1  # System instructions and role definition
    COGNITIVE_CONTINUITY = 2  # Lineage decisions, failed experiments, lessons learned
    ARCHITECTURAL_DRIFT = 3  # URGENT: Current violations, breaking changes
    REPOSITORY_SNAPSHOT = 4  # File structure, detected architecture, codebase state
    RETRIEVED_CONTEXT = 5  # Results from RetrievalPipeline (most droppable)
    WORKING_MEMORY = 6  # Current session turns, conversation history
    CURRENT_PROMPT = 7  # The immediate user request (always last)


@dataclass
class ContextChunk:
    """A chunk of assembled context with metadata for budget management.

    Chunks are grouped by section and trimmed based on trust_score and priority
    when the assembled context exceeds budget limits.
    """

    section: ContextSection
    content: str
    token_count: int
    source: str  # Debugging: where did this chunk come from?
    trust_score: float = 1.0  # 0.0 to 1.0 (lower = more likely to trim)
    priority: float = 0.5  # Within-section priority for trimming
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate chunk integrity."""
        if not (0.0 <= self.trust_score <= 1.0):
            raise ValueError(f"trust_score must be 0.0-1.0, got {self.trust_score}")
        if not self.content or not isinstance(self.content, str):
            raise ValueError("content must be a non-empty string")
        if self.token_count < 0:
            raise ValueError(f"token_count must be non-negative, got {self.token_count}")

    def is_trimmable(self) -> bool:
        """Check if this chunk can be trimmed when over budget.

        NEVER trim: SYSTEM_PROMPT, ARCHITECTURAL_DRIFT, CURRENT_PROMPT.
        """
        return self.section not in (
            ContextSection.SYSTEM_PROMPT,
            ContextSection.ARCHITECTURAL_DRIFT,
            ContextSection.CURRENT_PROMPT,
        )

    def trim_score(self) -> float:
        """Score for sorting chunks by trim-ability (lower = trim first).

        Lower scores are candidates for trimming. Combines trust_score
        (lower = less trustworthy) and priority (lower = less important).
        """
        return self.trust_score * self.priority


@dataclass
class ContextAssemblyReport:
    """Report of how context was assembled and trimmed."""

    total_chunks_received: int
    total_tokens_requested: int
    total_tokens_assembled: int
    sections_present: list[ContextSection] = field(default_factory=list)
    sections_trimmed: dict[ContextSection, int] = field(default_factory=dict)
    chunks_dropped: int = 0
    budget_exceeded: bool = False

    def __str__(self) -> str:
        """Human-readable assembly report."""
        lines = [
            "ContextAssemblyReport:",
            f"  Chunks: {self.total_chunks_received} received, {self.chunks_dropped} dropped",
            f"  Tokens: {self.total_tokens_assembled}/{self.total_tokens_requested}",
            f"  Sections: {len(self.sections_present)} present",
        ]
        if self.sections_trimmed:
            lines.append(f"  Trimmed: {self.sections_trimmed}")
        if self.budget_exceeded:
            lines.append("  WARNING: Budget exceeded even after trimming")
        return "\n".join(lines)
