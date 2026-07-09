"""Bootstrap factory for the Repository Intelligence Engine.

Registers RepositoryIntelligenceEngine as a Tier-1 (background warm)
SubsystemModule.  It depends on: knowledge_graph, repository_cognition,
and the event bus — all of which must be available in the container.
"""

from __future__ import annotations

from velune.kernel.bootstrap import RuntimeEnvironment, SubsystemModule


def _create_intelligence_engine(env: RuntimeEnvironment):
    from velune.events import CognitiveBus
    from velune.intelligence.engine import RepositoryIntelligenceEngine
    from velune.knowledge.graph import KnowledgeGraph
    from velune.repository.cognition import RepositoryCognitionService

    if not env.config.workspace.watch_files:
        return None

    # Resolve hard dependencies — fall back to stubs if not yet registered
    cognition: RepositoryCognitionService | None = env.container.get("runtime.repository_cognition")
    knowledge_graph: KnowledgeGraph | None = env.container.get("runtime.knowledge_graph")
    bus: CognitiveBus | None = env.container.get("runtime.bus")

    if cognition is None or knowledge_graph is None or bus is None:
        # Missing dependencies: log and return None so the engine is skipped
        import logging

        logging.getLogger("velune.intelligence.subsystems").warning(
            "RepositoryIntelligenceEngine skipped: missing dependencies "
            "(cognition=%s, knowledge_graph=%s, bus=%s)",
            cognition is not None,
            knowledge_graph is not None,
            bus is not None,
        )
        return None

    # Soft dependency: enables vector-store cleanup for removed files
    # (see engine.py::_handle_graph_patch). Absence just skips that step.
    retrieval = (
        env.container.get("runtime.retrieval") if env.container.has("runtime.retrieval") else None
    )

    return RepositoryIntelligenceEngine(
        workspace=env.workspace,
        cognition=cognition,
        knowledge_graph=knowledge_graph,
        bus=bus,
        retrieval=retrieval,
    )


INTELLIGENCE_MODULES = [
    SubsystemModule(
        name="repository_intelligence_engine",
        factory=_create_intelligence_engine,
        container_key="runtime.intelligence_engine",
        # A real lifecycle_key wires initialize()/shutdown() into
        # LifecycleCoordinator so change-detection, git-state polling, and
        # knowledge-graph orphan cleanup actually run — previously this was
        # None and the engine, even when constructed, was never started.
        # The factory above already degrades to a skipped module (returns
        # None) when its dependencies aren't ready, and initialize() itself
        # guards every fallible step (see engine.py), so this is safe to
        # treat as lifecycle-managed without making it startup-critical.
        lifecycle_key="repository_intelligence",
        dependencies=[
            "runtime.repository_cognition",
            "runtime.knowledge_graph",
            "runtime.bus",
            "runtime.retrieval",  # soft: ordering only, factory tolerates its absence
        ],
        tier=1,
    ),
]
