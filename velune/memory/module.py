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


def _create_lancedb_store(env: RuntimeEnvironment):
    from velune.core.paths import lancedb_store_path
    from velune.memory.storage.lancedb_store import LanceDBStore

    store = LanceDBStore(lancedb_store_path(env.workspace))
    if env.container.has("runtime.health_monitor"):
        monitor = env.container.get("runtime.health_monitor")
        monitor.register_health_hook("lancedb_store", store.health_check)
    return store


def _create_embedding_pipeline(env: RuntimeEnvironment):
    from velune.memory.embedding_pipeline import EmbeddingPipeline

    store = env.container.get("runtime.lancedb_store")
    try:
        provider_registry = env.container.get("runtime.provider_registry")
        provider = provider_registry.get("ollama") if provider_registry else None
    except Exception:
        provider = None
    return EmbeddingPipeline(provider, store)


def _create_semantic_memory_lance(env: RuntimeEnvironment):
    from velune.memory.tiers.semantic import SemanticMemory

    store = env.container.get("runtime.lancedb_store")
    pipeline = env.container.get("runtime.embedding_pipeline")
    return SemanticMemory(store, pipeline)


def _create_episodic_session_memory(env: RuntimeEnvironment):
    from velune.memory.tiers.episodic import EpisodicMemory

    pool = env.container.get("runtime.sqlite_pool")
    return EpisodicMemory(pool)


def _create_memory_lifecycle(env: RuntimeEnvironment):
    from velune.memory.lifecycle import MemoryLifecycleManager

    working_tier = env.container.get("runtime.working_memory")
    episodic_tier = env.container.get("runtime.episodic_memory")
    episodic_memory = env.container.get("runtime.episodic_session_memory")
    semantic_memory = env.container.get("runtime.semantic_memory_lance")
    embedding_pipeline = env.container.get("runtime.embedding_pipeline")
    lineage_tier = env.container.get("runtime.lineage_memory")

    manager = MemoryLifecycleManager(
        working_tier=working_tier,
        episodic_memory=episodic_memory,
        semantic_memory=semantic_memory,
        embedding_pipeline=embedding_pipeline,
        lineage_tier=lineage_tier,
        episodic_session_memory=episodic_tier,
    )

    # Register health hook if monitor is available
    if env.container.has("runtime.health_monitor"):
        monitor = env.container.get("runtime.health_monitor")

        async def _memory_health_check() -> dict:
            try:
                health = await manager.health()
                return health.to_dict()
            except Exception as e:
                return {"error": str(e)}

        # Bridge the async health() coroutine onto the synchronous health-hook
        # contract via submit(), which routes through the single run_async()
        # entry point in velune.kernel.entrypoint. submit() raises RuntimeError
        # when already inside a running event loop; in that case we return a
        # placeholder rather than blocking the loop with a nested run.
        def _sync_health_check() -> dict:
            from velune.core.event_loop import submit

            try:
                return submit(_memory_health_check())
            except RuntimeError:
                return {"status": "async_only"}
            except Exception as e:
                return {"error": str(e)}

        monitor.register_health_hook("memory", _sync_health_check)

    return manager


MEMORY_MODULES = [
    SubsystemModule(
        name="sqlite_pool",
        factory=_create_sqlite_pool,
        container_key="runtime.sqlite_pool",
        lifecycle_key="sqlite_pool",  # pool.initialize() → pool.startup()
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
        lifecycle_key="episodic_memory",  # tier.initialize() → _init_db()
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
        lifecycle_key="graph_memory",  # tier.initialize() → _init_db()
        dependencies=["runtime.sqlite_pool"],
    ),
    SubsystemModule(
        name="lineage_memory",
        factory=_create_lineage_tier,
        container_key="runtime.lineage_memory",
        lifecycle_key="lineage_memory",  # tier.initialize() → _init_db()
        dependencies=["runtime.sqlite_pool"],
    ),
    SubsystemModule(
        name="lancedb_store",
        factory=_create_lancedb_store,
        container_key="runtime.lancedb_store",
        lifecycle_key="lancedb_store",  # calls LanceDBStore.initialize()
    ),
    SubsystemModule(
        name="embedding_pipeline",
        factory=_create_embedding_pipeline,
        container_key="runtime.embedding_pipeline",
        lifecycle_key="embedding_pipeline",  # calls EmbeddingPipeline.initialize()
        dependencies=["runtime.lancedb_store"],
    ),
    SubsystemModule(
        name="semantic_memory_lance",
        factory=_create_semantic_memory_lance,
        container_key="runtime.semantic_memory_lance",
        dependencies=["runtime.lancedb_store", "runtime.embedding_pipeline"],
    ),
    SubsystemModule(
        name="episodic_session_memory",
        factory=_create_episodic_session_memory,
        container_key="runtime.episodic_session_memory",
        lifecycle_key="episodic_session_memory",  # calls EpisodicMemory.initialize()
        dependencies=["runtime.sqlite_pool"],
    ),
    SubsystemModule(
        name="memory_lifecycle",
        factory=_create_memory_lifecycle,
        container_key="runtime.memory_lifecycle",
        lifecycle_key="memory",
        dependencies=[
            "runtime.working_memory",
            "runtime.episodic_memory",
            "runtime.episodic_session_memory",
            "runtime.semantic_memory_lance",
            "runtime.embedding_pipeline",
            "runtime.lineage_memory",
        ],
    ),
]
