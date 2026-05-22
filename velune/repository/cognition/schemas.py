"""Schemas for repository cognition indexing."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class RepositoryLanguage(str, Enum):
    """Detected source language."""

    PYTHON = "python"
    TYPESCRIPT = "typescript"
    JAVASCRIPT = "javascript"
    GO = "go"
    RUST = "rust"
    UNKNOWN = "unknown"


class RepositorySymbolKind(str, Enum):
    """Repository symbol kinds."""

    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    VARIABLE = "variable"
    IMPORT = "import"
    MODULE = "module"


class RepositoryFile(BaseModel):
    """File-level repository node."""

    path: str
    language: RepositoryLanguage = RepositoryLanguage.UNKNOWN
    size_bytes: int = 0
    sha256: Optional[str] = None
    owners: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RepositorySymbol(BaseModel):
    """Symbol extracted from a file."""

    name: str
    kind: RepositorySymbolKind
    file_path: str
    line_start: int = 0
    line_end: int = 0
    parent: Optional[str] = None
    imports: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RepositoryEdge(BaseModel):
    """Graph edge between repository entities."""

    source: str
    target: str
    edge_type: str
    weight: float = 1.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class RepositorySnapshot(BaseModel):
    """Result of a repository cognition pass."""

    root_path: str
    files: list[RepositoryFile] = Field(default_factory=list)
    symbols: list[RepositorySymbol] = Field(default_factory=list)
    edges: list[RepositoryEdge] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))