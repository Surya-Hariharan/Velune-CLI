"""Hierarchical Memory Subsystem for Velune Cognitive OS.

Includes working (Tier 1), episodic (Tier 2), semantic (Tier 3), graph (Tier 4)
and lineage memory systems along with prioritized lifecycle managers.

The Three-Brain Coordinator provides a unified query interface across all tiers.
"""

from velune.memory.lifecycle import MemoryLifecycleManager
from velune.memory.three_brain import ThreeBrainCoordinator, ThreeBrainResult
from velune.memory.tiers.episodic import EpisodicMemoryTier, EpisodicStep, EpisodicTurn
from velune.memory.tiers.graph import GraphEdge, GraphMemoryTier, GraphNode
from velune.memory.tiers.lineage import LineageMemoryTier
from velune.memory.tiers.semantic import SemanticMemoryTier
from velune.memory.tiers.working import MemoryTurn, WorkingMemoryTier

__all__ = [
    "WorkingMemoryTier",
    "MemoryTurn",
    "EpisodicMemoryTier",
    "EpisodicTurn",
    "EpisodicStep",
    "SemanticMemoryTier",
    "GraphMemoryTier",
    "GraphNode",
    "GraphEdge",
    "LineageMemoryTier",
    # Three-Brain Coordinator — unified interface across all memory tiers
    "ThreeBrainCoordinator",
    "ThreeBrainResult",
    # Lifecycle manager for multi-tier retrieval, vitality-based filtering,
    # and health reporting.
    "MemoryLifecycleManager",
]
