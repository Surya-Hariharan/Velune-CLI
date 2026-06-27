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

    # Resolve hard dependencies — fall back to stubs if not yet registered
    cognition: RepositoryCognitionService | None = env.container.get("runtime.repository_cognition")
    knowledge_graph: KnowledgeGraph | None = env.container.get("runtime.knowledge_graph")
    bus: CognitiveBus | None = env.container.get("runtime.event_bus")

    if cognition is None or knowledge_graph is None or bus is None:
        # Missing dependencies: log and return None so the engine is skipped
        import logging

        logging.getLogger("velune.intelligence.module").warning(
            "RepositoryIntelligenceEngine skipped: missing dependencies "
            "(cognition=%s, knowledge_graph=%s, bus=%s)",
            cognition is not None,
            knowledge_graph is not None,
            bus is not None,
        )
        return None

    return RepositoryIntelligenceEngine(
        workspace=env.workspace,
        cognition=cognition,
        knowledge_graph=knowledge_graph,
        bus=bus,
    )


INTELLIGENCE_MODULES = [
    SubsystemModule(
        name="repository_intelligence_engine",
        factory=_create_intelligence_engine,
        container_key="runtime.intelligence_engine",
        lifecycle_key=None,  # Non-critical: engine absence degrades gracefully
        dependencies=[
            "runtime.repository_cognition",
            "runtime.knowledge_graph",
            "runtime.event_bus",
        ],
        tier=1,
    ),
]
