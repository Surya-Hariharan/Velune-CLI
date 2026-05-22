"""Core repository type definitions."""

from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


class FileNode(BaseModel):
    """A file node in the repository."""
    path: str
    language: Optional[str] = None
    size_bytes: int
    last_modified: float
    is_ignored: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class SymbolNode(BaseModel):
    """A symbol node (function, class, variable) in the repository."""
    name: str
    kind: str  # function, class, variable, etc.
    file_path: str
    line_start: int
    line_end: int
    parent: Optional[str] = None
    children: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DependencyEdge(BaseModel):
    """A dependency edge between nodes."""
    source: str
    target: str
    edge_type: str  # import, call, inheritance, etc.
    metadata: dict[str, Any] = Field(default_factory=dict)
