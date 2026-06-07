from velune.kernel.bootstrap import RuntimeEnvironment, SubsystemModule


def _create_sqlite_manager(env: RuntimeEnvironment):
    from velune.core.paths import cognitive_db_path, migrate_legacy_storage
    from velune.memory.storage.sqlite_manager import SQLiteManager
    # First subsystem to touch heavy state — run the one-time relocation of
    # any pre-existing in-workspace (possibly cloud-synced) data.
    migrate_legacy_storage(env.workspace)
    return SQLiteManager(cognitive_db_path(env.workspace))

def _create_working_tier(env: RuntimeEnvironment):
    from velune.memory.tiers.working import WorkingMemoryTier
    return WorkingMemoryTier()

def _create_episodic_tier(env: RuntimeEnvironment):
    from velune.core.paths import cognitive_db_path
    from velune.memory.tiers.episodic import EpisodicMemoryTier
    db_path = cognitive_db_path(env.workspace)
    sqlite_manager = env.container.get("runtime.sqlite_manager")
    return EpisodicMemoryTier(db_path, sqlite_manager=sqlite_manager)

def _create_semantic_tier(env: RuntimeEnvironment):
    from velune.core.paths import qdrant_store_path
    from velune.memory.tiers.semantic import SemanticMemoryTier
    return SemanticMemoryTier(path=str(qdrant_store_path(env.workspace)))

def _create_graph_tier(env: RuntimeEnvironment):
    from velune.core.paths import cognitive_db_path
    from velune.memory.tiers.graph import GraphMemoryTier
    db_path = cognitive_db_path(env.workspace)
    sqlite_manager = env.container.get("runtime.sqlite_manager")
    return GraphMemoryTier(db_path, sqlite_manager=sqlite_manager)

def _create_lineage_tier(env: RuntimeEnvironment):
    from velune.core.paths import cognitive_db_path
    from velune.memory.tiers.lineage import LineageMemoryTier
    sqlite_manager = env.container.get("runtime.sqlite_manager")
    return LineageMemoryTier(
        db_path=cognitive_db_path(env.workspace),
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
