"""Retrieval data contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from velune._compat import StrEnum


class RetrievalSource(StrEnum):
    """Source of a retrieval hit."""

    VECTOR = "vector"
    LEXICAL = "lexical"
    GRAPH = "graph"
    MEMORY = "memory"


class RetrievalDocument(BaseModel):
    """Document stored in retrieval indexes."""

    id: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    namespace: str = "default"
    embedding: list[float] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))


class RetrievalHit(BaseModel):
    """A ranked retrieval hit."""

    document: RetrievalDocument
    score: float
    source: RetrievalSource
    rank: int = 0


class RetrievalQuery(BaseModel):
    """Query for hybrid retrieval."""

    text: str
    top_k: int = Field(default=10, ge=1, le=100)
    namespace: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    vector_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    lexical_weight: float = Field(default=0.3, ge=0.0, le=1.0)
    graph_weight: float = Field(default=0.2, ge=0.0, le=1.0)
    # Set by RetrievalPlanner.plan() so the reranker can apply an
    # intent-conditioned trust boost (see CrossEncoderReranker). Optional and
    # unused by default — direct RetrievalQuery construction (e.g. `velune
    # retrieval trace`) behaves exactly as before this field existed.
    intent: str | None = None


class RetrievalResult(BaseModel):
    """Retrieved items plus provenance metadata."""

    query: RetrievalQuery
    hits: list[RetrievalHit] = Field(default_factory=list)
    strategy: str = "hybrid"
    metadata: dict[str, Any] = Field(default_factory=dict)
