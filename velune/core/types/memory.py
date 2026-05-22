"""Core memory type definitions."""

from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field
from datetime import datetime


class MemoryType(str, Enum):
    """Types of memory records."""
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    GRAPH = "graph"


class MemoryRecord(BaseModel):
    """A memory record."""
    id: str
    memory_type: MemoryType
    content: str
    embedding: Optional[list[float]] = None
    importance: float = Field(ge=0.0, le=1.0)
    access_count: int = 0
    last_accessed: datetime
    created_at: datetime
    expires_at: Optional[datetime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryQuery(BaseModel):
    """Query for memory retrieval."""
    query_text: str
    memory_types: list[MemoryType] = Field(default_factory=list)
    limit: int = Field(default=10, ge=1, le=100)
    min_importance: float = Field(default=0.0, ge=0.0, le=1.0)
    metadata_filter: dict[str, Any] = Field(default_factory=dict)
