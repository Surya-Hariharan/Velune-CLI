"""Bootstrap factory for the knowledge subsystem.

Registers KnowledgeGraph and KnowledgeQuery as Tier-1 (background warm)
subsystems via the standard SubsystemModule protocol.
"""

from __future__ import annotations

from velune.kernel.bootstrap import RuntimeEnvironment, SubsystemModule

_DB_FILENAME = "knowledge_graph.db"


def _create_knowledge_graph(env: RuntimeEnvironment):
    from velune.knowledge.graph import KnowledgeGraph

    db_path = env.workspace / ".velune" / _DB_FILENAME
    return KnowledgeGraph(db_path)


def _create_knowledge_query(env: RuntimeEnvironment):
    from velune.knowledge.query import KnowledgeQuery

    graph: object = env.container.get("runtime.knowledge_graph")
    if graph is None:
        from velune.knowledge.graph import KnowledgeGraph

        db_path = env.workspace / ".velune" / _DB_FILENAME
        graph = KnowledgeGraph(db_path)
    return KnowledgeQuery(graph)  # type: ignore[arg-type]


KNOWLEDGE_MODULES = [
    SubsystemModule(
        name="knowledge_graph",
        factory=_create_knowledge_graph,
        container_key="runtime.knowledge_graph",
        lifecycle_key=None,  # Non-critical: graph absence degrades gracefully
        tier=1,
    ),
    SubsystemModule(
        name="knowledge_query",
        factory=_create_knowledge_query,
        container_key="runtime.knowledge_query",
        lifecycle_key=None,
        dependencies=["runtime.knowledge_graph"],
        tier=1,
    ),
]
