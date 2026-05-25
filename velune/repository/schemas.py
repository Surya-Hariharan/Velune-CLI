"""Strictly-typed schemas for repository cognition."""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RepositoryLanguage(str, Enum):
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    GO = "go"
    RUST = "rust"
    UNKNOWN = "unknown"


class RepositorySymbolKind(str, Enum):
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    IMPORT = "import"
    UNKNOWN = "unknown"


class RepositorySymbol(BaseModel):
    name: str
    kind: RepositorySymbolKind
    file_path: str
    line_start: int = 1
    line_end: int = 1
    docstring: str | None = None
    parent: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RepositoryFile(BaseModel):
    path: str
    language: RepositoryLanguage
    size_bytes: int
    sha256: str
    symbols: list[RepositorySymbol] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RepositoryEdge(BaseModel):
    source: str
    target: str
    edge_type: str  # e.g., "imports", "calls", "contains"
    weight: float = 1.0


class RepositorySnapshot(BaseModel):
    root_path: str
    files: list[RepositoryFile] = Field(default_factory=list)
    symbols: list[RepositorySymbol] = Field(default_factory=list)
    edges: list[RepositoryEdge] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
