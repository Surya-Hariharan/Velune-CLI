"""Repository Knowledge Graph — Sprint 1 / AI Foundation.

Provides an AI-queryable semantic graph of the codebase: files, modules,
classes, functions, and the relationships between them.

Key classes
-----------
KnowledgeGraph      Async SQLite-backed persistent graph store.
KnowledgeGraphBuilder  Builds the graph from a RepositorySnapshot.
KnowledgeQuery      Higher-level AI-optimized queries.
KnowledgeNode       A node (file, class, function, …).
KnowledgeEdge       A directed relationship between nodes.
NodeType            Enumeration of node categories.
EdgeType            Enumeration of relationship types.
KnowledgeGraphStats Summary statistics for the graph.

Storage
-------
Persists to ``<workspace>/.velune/knowledge_graph.db`` (SQLite, WAL mode).
Completely separate from the memory subsystem's cognitive-state store.

Bootstrap
---------
Register ``KNOWLEDGE_MODULES`` with the ``RuntimeBootstrapper`` to get
``KnowledgeGraph`` and ``KnowledgeQuery`` injected as Tier-1 services.
"""

from velune.knowledge.builder import KnowledgeGraphBuilder
from velune.knowledge.graph import KnowledgeGraph
from velune.knowledge.module import KNOWLEDGE_MODULES
from velune.knowledge.query import FileContext, KnowledgeQuery, SubgraphContext
from velune.knowledge.schemas import (
    EdgeType,
    KnowledgeEdge,
    KnowledgeGraphStats,
    KnowledgeNode,
    NodeType,
)

__all__ = [
    # Core graph
    "KnowledgeGraph",
    # Builder
    "KnowledgeGraphBuilder",
    # Query layer
    "KnowledgeQuery",
    "FileContext",
    "SubgraphContext",
    # Schemas
    "KnowledgeNode",
    "KnowledgeEdge",
    "NodeType",
    "EdgeType",
    "KnowledgeGraphStats",
    # Bootstrap
    "KNOWLEDGE_MODULES",
]
