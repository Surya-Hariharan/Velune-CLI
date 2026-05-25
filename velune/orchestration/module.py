from velune.kernel.bootstrap import RuntimeEnvironment, SubsystemModule


def _create_orchestration_engine(env: RuntimeEnvironment):
    from velune.orchestration.engine import LangGraphOrchestrationEngine

    retrieval = env.container.get("runtime.retrieval")
    repository_cognition = env.container.get("runtime.repository_cognition")
    memory_lifecycle = env.container.get("runtime.memory_lifecycle")
    graph_memory = env.container.get("runtime.graph_memory")
    tool_registry = env.container.get("runtime.tool_registry")

    return LangGraphOrchestrationEngine(
        retrieval=retrieval,
        repository_cognition=repository_cognition,
        memory_lifecycle=memory_lifecycle,
        graph_memory=graph_memory,
        tool_registry=tool_registry,
        workspace_path=env.workspace,
    )

ORCHESTRATION_MODULES = [
    SubsystemModule(
        name="orchestration_engine",
        factory=_create_orchestration_engine,
        container_key="runtime.orchestration_engine",
        dependencies=[
            "runtime.retrieval",
            "runtime.repository_cognition",
            "runtime.memory_lifecycle",
            "runtime.graph_memory",
            "runtime.tool_registry",
        ],
    )
]
