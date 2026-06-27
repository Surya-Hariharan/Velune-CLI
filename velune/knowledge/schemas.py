"""Schemas for the Repository Knowledge Graph.

Node types and edge types model the semantic structure of a codebase —
files, modules, classes, functions, and the relationships between them.
These are distinct from the memory/tiers/graph.py schemas, which model
AI cognitive state rather than code structure.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

from velune._compat import StrEnum


class NodeType(StrEnum):
    FILE = "file"
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"


class EdgeType(StrEnum):
    IMPORTS = "imports"
    CONTAINS = "contains"
    INHERITS = "inherits"
    DEFINES = "defines"


class KnowledgeNode(BaseModel):
    """A single entity in the Repository Knowledge Graph."""

    id: str
    node_type: NodeType
    label: str
    file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeEdge(BaseModel):
    """A directed relationship between two knowledge graph nodes."""

    source: str
    target: str
    edge_type: EdgeType
    weight: float = 1.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeGraphStats(BaseModel):
    """Summary statistics for a built knowledge graph."""

    node_count: int = 0
    edge_count: int = 0
    file_count: int = 0
    symbol_count: int = 0
    root_path: str = ""
    built_at: float = Field(default_factory=time.time)
