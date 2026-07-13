"""Strictly-typed schemas for repository cognition."""

import hashlib
from typing import Any

from pydantic import BaseModel, Field, model_validator

from velune._compat import StrEnum


def build_qualified_name(file_path: str, name: str, parent: str | None = None) -> str:
    """Builds a dotted qualified name for a symbol from its file path and parent scope."""
    p = file_path.replace("\\", "/")

    # Extract package path relative to 'velune/' if absolute
    if "/velune/" in p:
        p = p.split("/velune/", 1)[1]
        p = "velune/" + p
    elif p.startswith("c:") or p.startswith("C:") or ":" in p:
        p = p.split(":", 1)[1].lstrip("/")

    p = p.rsplit(".", 1)[0]
    dotted = p.replace("/", ".").strip(".")

    if parent:
        return f"{dotted}.{parent}.{name}"
    return f"{dotted}.{name}"


def compute_symbol_id(file_path: str, qualified_name: str, kind: str) -> str:
    """Computes a stable, deterministic, line-independent SHA256 identity for a symbol."""
    p = file_path.replace("\\", "/")
    if "/velune/" in p:
        p = p.split("/velune/", 1)[1]
        p = "velune/" + p
    elif p.startswith("c:") or p.startswith("C:") or ":" in p:
        p = p.split(":", 1)[1].lstrip("/")

    payload = f"{p}:{qualified_name}:{kind.lower()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RepositoryLanguage(StrEnum):
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    GO = "go"
    RUST = "rust"
    JAVA = "java"
    # C and C++ share one bucket: headers are indistinguishable by extension
    # and the regex symbol patterns overlap.
    CPP = "cpp"
    UNKNOWN = "unknown"


class RepositorySymbolKind(StrEnum):
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
    symbol_id: str | None = None
    qualified_name: str | None = None

    @model_validator(mode="after")
    def populate_identity(self) -> "RepositorySymbol":
        """Ensures stable symbol_id and qualified_name are automatically calculated if missing."""
        if not self.qualified_name:
            self.qualified_name = build_qualified_name(self.file_path, self.name, self.parent)
        if not self.symbol_id:
            self.symbol_id = compute_symbol_id(self.file_path, self.qualified_name, self.kind.value)
        return self


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

    # The API connection map (routes, frontend calls, DB queries) built by
    # repository/api_mapper.py. Declared here because it must be: this is a
    # Pydantic v2 model, so the previous `snapshot.api_map = api_map` assignment
    # on an undeclared attribute raised ValueError on *every* index run. The
    # raise was swallowed by a broad `except Exception` upstream, so the whole
    # API-map feature was silently dead.
    #
    # Typed `Any` rather than `APIConnectionMap` on purpose: that type lives in
    # api_mapper.py, a heavy regex-laden module, and schemas.py is imported
    # nearly everywhere. The map is only ever read back via attribute access,
    # never validated or serialised, so the import cost buys nothing.
    api_map: Any = None
