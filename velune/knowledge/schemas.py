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
    # A fused, higher-order intent claim ("this cluster implements HTTP
    # request validation") — distinct from the structural node types above,
    # which describe *what calls what*, not *what the system does*. See
    # docs/repository-intelligence-baseline.md's discussion of a semantic
    # layer above the structural graph.
    CAPABILITY = "capability"
    # A statically-identified execution entrypoint (CLI command, HTTP route
    # table, message-queue consumer, scheduled job, `if __name__ ==
    # "__main__"` block) — what actually runs, as distinct from what merely
    # parses.
    RUNTIME_ENTRYPOINT = "runtime_entrypoint"


class EdgeType(StrEnum):
    IMPORTS = "imports"
    CONTAINS = "contains"
    INHERITS = "inherits"
    DEFINES = "defines"
    # A call-graph edge (as opposed to CONTAINS' structural nesting) — was
    # previously recognized by the repo-edge→KG-edge mapping
    # (graph_patcher._repo_edge_to_kg_type) but mapped to None and silently
    # dropped; now a first-class edge type.
    CALLS = "calls"
    # A test file/function's relationship to the production code it
    # exercises.
    TESTS = "tests"
    # Git lineage: this node's current form evolved from an earlier one
    # (e.g. after a rename — see incremental_indexer.IndexDelta.renames —
    # or a tracked refactor).
    EVOLVED_FROM = "evolved_from"


class KnowledgeNode(BaseModel):
    """A single entity in the Repository Knowledge Graph."""

    id: str
    node_type: NodeType
    label: str
    file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Confidence-scored, not implicitly-certain — see
    # velune.repository.schemas.Claim/ClaimAccumulator, the same idea
    # applied to graph nodes instead of classifier outputs. 1.0 (certain)
    # for nodes derived directly from static structure (a FILE/FUNCTION/
    # METHOD parsed from real source); lower for inferred nodes (a
    # CAPABILITY an LLM proposed, a RUNTIME_ENTRYPOINT found only by naming
    # convention rather than an explicit framework pattern).
    confidence: float = 1.0
    # Which signal(s)/pipeline stage(s) produced this node — e.g.
    # ["ast_parse"], ["decorator_shape", "package_manifest"]. Mirrors
    # Claim.source_signals; lets a caller explain *why* a node exists, not
    # just assert that it does.
    provenance: list[str] = Field(default_factory=list)


class KnowledgeEdge(BaseModel):
    """A directed relationship between two knowledge graph nodes."""

    source: str
    target: str
    edge_type: EdgeType
    weight: float = 1.0
    metadata: dict[str, Any] = Field(default_factory=dict)
    # See KnowledgeNode.confidence/provenance — the same confidence-scored,
    # explainable-provenance treatment applied to edges. A statically-
    # resolved IMPORTS edge is 1.0; an IMPORTS_DYNAMIC-derived edge (see
    # repository.parser._extract_dynamic_imports) carries its resolution
    # confidence through here instead of only living in `weight`.
    confidence: float = 1.0
    provenance: list[str] = Field(default_factory=list)


class KnowledgeGraphStats(BaseModel):
    """Summary statistics for a built knowledge graph."""

    node_count: int = 0
    edge_count: int = 0
    file_count: int = 0
    symbol_count: int = 0
    root_path: str = ""
    built_at: float = Field(default_factory=time.time)
