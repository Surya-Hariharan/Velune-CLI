from velune.kernel.bootstrap import RuntimeEnvironment, SubsystemModule


def _create_repository_cognition(env: RuntimeEnvironment):
    from velune.repository.cognition import RepositoryCognitionService
    return RepositoryCognitionService(env.workspace)

def _create_workspace_watcher(env: RuntimeEnvironment):
    from velune.repository.watcher import WorkspaceEvolutionWatcher
    cognition = env.container.get("runtime.repository_cognition")
    semantic_memory = env.container.get("runtime.semantic_memory") if env.container.has("runtime.semantic_memory") else None
    event_bus = env.container.get("runtime.bus") if env.container.has("runtime.bus") else None

    return WorkspaceEvolutionWatcher(
        root_path=env.workspace,
        indexer=cognition.indexer,
        grapher=cognition.grapher,
        semantic_memory=semantic_memory,
        event_bus=event_bus,
    )

REPOSITORY_MODULES = [
    SubsystemModule(
        name="repository_cognition",
        factory=_create_repository_cognition,
        container_key="runtime.repository_cognition",
        lifecycle_key="repository",
    ),
    SubsystemModule(
        name="workspace_watcher",
        factory=_create_workspace_watcher,
        container_key="runtime.workspace_watcher",
        lifecycle_key="workspace_watcher",
        dependencies=["runtime.repository_cognition"],
    )
]
