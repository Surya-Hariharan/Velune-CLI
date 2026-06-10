from velune.kernel.bootstrap import RuntimeEnvironment, SubsystemModule


def _create_sqlite_pool(env: RuntimeEnvironment):
    from velune.core.paths import cognitive_db_path, migrate_legacy_storage
    from velune.memory.storage.sqlite_pool import SQLiteConnectionPool
    # One-time relocation of any pre-existing in-workspace (cloud-synced) data.
    migrate_legacy_storage(env.workspace)
    pool = SQLiteConnectionPool(cognitive_db_path(env.workspace))
    # Register is_healthy as a custom health hook when the monitor is available.
    if env.container.has("runtime.health_monitor"):
        monitor = env.container.get("runtime.health_monitor")
        monitor.register_health_hook("sqlite_pool", pool.health_check)
    return pool


def _create_working_tier(env: RuntimeEnvironment):
    from velune.memory.tiers.working import WorkingMemoryTier
    return WorkingMemoryTier()


def _create_episodic_tier(env: RuntimeEnvironment):
    from velune.memory.tiers.episodic import EpisodicMemoryTier
    pool = env.container.get("runtime.sqlite_pool")
    return EpisodicMemoryTier(pool)


def _create_semantic_tier(env: RuntimeEnvironment):
    from velune.core.paths import qdrant_store_path
    from velune.memory.tiers.semantic import SemanticMemoryTier
    return SemanticMemoryTier(path=str(qdrant_store_path(env.workspace)))


def _create_graph_tier(env: RuntimeEnvironment):
    from velune.memory.tiers.graph import GraphMemoryTier
    pool = env.container.get("runtime.sqlite_pool")
    return GraphMemoryTier(pool)


def _create_lineage_tier(env: RuntimeEnvironment):
    from velune.memory.tiers.lineage import LineageMemoryTier
    pool = env.container.get("runtime.sqlite_pool")
    return LineageMemoryTier(pool)


def _create_memory_lifecycle(env: RuntimeEnvironment):
    from velune.memory.lifecycle import MemoryLifecycleCoordinator
    working_tier = env.container.get("runtime.working_memory")
    episodic_tier = env.container.get("runtime.episodic_memory")
    return MemoryLifecycleCoordinator(working_tier, episodic_tier)


MEMORY_MODULES = [
    SubsystemModule(
        name="sqlite_pool",
        factory=_create_sqlite_pool,
        container_key="runtime.sqlite_pool",
        lifecycle_key="sqlite_pool",            # pool.initialize() → pool.startup()
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
        lifecycle_key="episodic_memory",        # tier.initialize() → _init_db()
        dependencies=["runtime.sqlite_pool"],
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
        lifecycle_key="graph_memory",           # tier.initialize() → _init_db()
        dependencies=["runtime.sqlite_pool"],
    ),
    SubsystemModule(
        name="lineage_memory",
        factory=_create_lineage_tier,
        container_key="runtime.lineage_memory",
        lifecycle_key="lineage_memory",         # tier.initialize() → _init_db()
        dependencies=["runtime.sqlite_pool"],
    ),
    SubsystemModule(
        name="memory_lifecycle",
        factory=_create_memory_lifecycle,
        container_key="runtime.memory_lifecycle",
        lifecycle_key="memory",
        dependencies=["runtime.working_memory", "runtime.episodic_memory"],
    ),
]
