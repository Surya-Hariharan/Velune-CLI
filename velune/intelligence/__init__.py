"""Repository Intelligence Engine — central coordinator for repository knowledge.

This package wires together change detection, event emission, incremental
Knowledge Graph patching, git state tracking, and downstream scheduling into
one lifecycle-managed service.

Key classes
-----------
RepositoryIntelligenceEngine
    The main coordinator.  Initialize once; subscribe to its events via
    the CognitiveBus rather than polling state directly.

KnowledgeGraphPatcher
    Surgical incremental updater for the KnowledgeGraph.  Used internally
    by the engine; exposed here for direct use in tests and tools.

RepositoryEventType
    Canonical event-type string constants.

Bootstrap
---------
Register ``INTELLIGENCE_MODULES`` with the ``RuntimeBootstrapper`` to get
``RepositoryIntelligenceEngine`` injected as a Tier-1 background service.

Event taxonomy
--------------
``repository.files_changed``          — files added/updated/removed
``repository.index_updated``          — IndexState persisted to disk
``repository.knowledge_graph_patched``— KG nodes/edges changed
``repository.git_state_changed``      — branch, SHA, or uncommitted count changed
``repository.profile_refreshed``      — tech stack / project type refreshed
``repository.engine_started``         — engine lifecycle
``repository.engine_stopped``         — engine lifecycle
"""

from velune.intelligence.engine import RepositoryIntelligenceEngine
from velune.intelligence.events import (
    RepositoryEventType,
    make_engine_started,
    make_engine_stopped,
    make_files_changed,
    make_git_state_changed,
    make_index_updated,
    make_knowledge_graph_patched,
    make_profile_refreshed,
)
from velune.intelligence.graph_patcher import KnowledgeGraphPatcher, PatchResult
from velune.intelligence.subsystems import INTELLIGENCE_MODULES

__all__ = [
    # Engine
    "RepositoryIntelligenceEngine",
    # Patcher
    "KnowledgeGraphPatcher",
    "PatchResult",
    # Events
    "RepositoryEventType",
    "make_files_changed",
    "make_index_updated",
    "make_knowledge_graph_patched",
    "make_git_state_changed",
    "make_profile_refreshed",
    "make_engine_started",
    "make_engine_stopped",
    # Bootstrap
    "INTELLIGENCE_MODULES",
]
