"""Core context type definitions."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ContextPriority(StrEnum):
    """Context chunk priority levels."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ContextChunk(BaseModel):
    """A chunk of context with metadata."""

    content: str
    source: str
    priority: ContextPriority
    tokens: int
    relevance_score: float = Field(ge=0.0, le=1.0)
    timestamp: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextWindow(BaseModel):
    """The assembled context window for inference."""

    chunks: list[ContextChunk]
    total_tokens: int
    max_tokens: int
    compression_ratio: float = Field(ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def utilization(self) -> float:
        """Calculate context window utilization."""
        return self.total_tokens / self.max_tokens if self.max_tokens > 0 else 0.0
