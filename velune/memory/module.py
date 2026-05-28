from velune.kernel.bootstrap import RuntimeEnvironment, SubsystemModule


def _create_sqlite_manager(env: RuntimeEnvironment):
    from velune.memory.storage.sqlite_manager import SQLiteManager
    velune_dir = env.workspace / ".velune"
    velune_dir.mkdir(parents=True, exist_ok=True)
    db_path = velune_dir / "velune_cognitive_core.db"
    return SQLiteManager(db_path)

def _create_working_tier(env: RuntimeEnvironment):
    from velune.memory.tiers.working import WorkingMemoryTier
    return WorkingMemoryTier()

def _create_episodic_tier(env: RuntimeEnvironment):
    from velune.memory.tiers.episodic import EpisodicMemoryTier
    velune_dir = env.workspace / ".velune"
    velune_dir.mkdir(parents=True, exist_ok=True)
    db_path = velune_dir / "velune_cognitive_core.db"
    sqlite_manager = env.container.get("runtime.sqlite_manager")
    return EpisodicMemoryTier(db_path, sqlite_manager=sqlite_manager)

def _create_semantic_tier(env: RuntimeEnvironment):
    from velune.memory.tiers.semantic import SemanticMemoryTier
    velune_dir = env.workspace / ".velune"
    velune_dir.mkdir(parents=True, exist_ok=True)
    vector_path = str(velune_dir / "qdrant_local_store")
    return SemanticMemoryTier(path=vector_path)

def _create_graph_tier(env: RuntimeEnvironment):
    from velune.memory.tiers.graph import GraphMemoryTier
    velune_dir = env.workspace / ".velune"
    velune_dir.mkdir(parents=True, exist_ok=True)
    db_path = velune_dir / "velune_cognitive_core.db"
    sqlite_manager = env.container.get("runtime.sqlite_manager")
    return GraphMemoryTier(db_path, sqlite_manager=sqlite_manager)

def _create_lineage_tier(env: RuntimeEnvironment):
    from velune.memory.tiers.lineage import LineageMemoryTier
    sqlite_manager = env.container.get("runtime.sqlite_manager")
    return LineageMemoryTier(
        db_path=env.workspace / ".velune" / "velune_cognitive_core.db",
        sqlite_manager=sqlite_manager,
    )

def _create_memory_lifecycle(env: RuntimeEnvironment):
    from velune.memory.lifecycle import MemoryLifecycleCoordinator
    working_tier = env.container.get("runtime.working_memory")
    episodic_tier = env.container.get("runtime.episodic_memory")
    return MemoryLifecycleCoordinator(working_tier, episodic_tier)

MEMORY_MODULES = [
    SubsystemModule(
        name="sqlite_manager",
        factory=_create_sqlite_manager,
        container_key="runtime.sqlite_manager",
        lifecycle_key="sqlite_manager",
    ),
    SubsystemModule(
        name="working_memory",
        factory=_create_working_tier,
        container_key="runtime.working_memory",
    ),
    SubsystemModule(
        name="episodic_memory",
        factory=_create_episodic_tier,
        container_key="runtime.episodic_memory",
        dependencies=["runtime.sqlite_manager"],
    ),
    SubsystemModule(
        name="semantic_memory",
        factory=_create_semantic_tier,
        container_key="runtime.semantic_memory",
    ),
    SubsystemModule(
        name="graph_memory",
        factory=_create_graph_tier,
        container_key="runtime.graph_memory",
        dependencies=["runtime.sqlite_manager"],
    ),
    SubsystemModule(
        name="lineage_memory",
        factory=_create_lineage_tier,
        container_key="runtime.lineage_memory",
        dependencies=["runtime.sqlite_manager"],
    ),
    SubsystemModule(
        name="memory_lifecycle",
        factory=_create_memory_lifecycle,
        container_key="runtime.memory_lifecycle",
        lifecycle_key="memory",
        dependencies=["runtime.working_memory", "runtime.episodic_memory"],
    ),
]
