from velune.kernel.bootstrap import RuntimeEnvironment, SubsystemModule


def _create_sqlite_pool(env: RuntimeEnvironment):
    from velune.core.paths import cognitive_db_path, migrate_legacy_storage
    from velune.memory.storage.sqlite_pool import SQLiteConnectionPool

    # One-time relocation of any pre-existing in-workspace (cloud-synced) data.
    migrate_legacy_storage(env.workspace)
    pool = SQLiteConnectionPool(cognitive_db_path(env.workspace))
    return pool


def _create_working_tier(env: RuntimeEnvironment):
    from velune.memory.tiers.working import WorkingMemoryTier

    return WorkingMemoryTier()


def _create_episodic_tier(env: RuntimeEnvironment):
    from velune.memory.tiers.episodic import EpisodicMemoryTier

    pool = env.container.get("runtime.sqlite_pool")
    return EpisodicMemoryTier(pool)


def _create_code_vector_connection(env: RuntimeEnvironment):
    from velune.core.paths import qdrant_store_path
    from velune.memory.tiers.semantic import CodeVectorConnection

    return CodeVectorConnection(path=str(qdrant_store_path(env.workspace)))


def _create_lineage_tier(env: RuntimeEnvironment):
    from velune.memory.tiers.lineage import LineageMemoryTier

    pool = env.container.get("runtime.sqlite_pool")
    return LineageMemoryTier(pool)


def _create_lancedb_store(env: RuntimeEnvironment):
    from velune.core.paths import lancedb_store_path
    from velune.memory.storage.lancedb_store import LanceDBStore

    return LanceDBStore(lancedb_store_path(env.workspace))


def _create_embedding_pipeline(env: RuntimeEnvironment):
    from velune.memory.embedding_pipeline import EmbeddingPipeline

    store = env.container.get("runtime.lancedb_store")
    try:
        provider_registry = env.container.get("runtime.provider_registry")
        provider = provider_registry.get("ollama") if provider_registry else None
    except Exception:
        provider = None

    profile = env.container.get_optional("runtime.profile")
    kwargs = {"concurrency": profile.background_concurrency} if profile else {}
    return EmbeddingPipeline(provider, store, **kwargs)


def _create_semantic_memory_lance(env: RuntimeEnvironment):
    from velune.memory.tiers.semantic import SemanticMemory

    store = env.container.get("runtime.lancedb_store")
    pipeline = env.container.get("runtime.embedding_pipeline")
    return SemanticMemory(store, pipeline)


def _create_episodic_session_memory(env: RuntimeEnvironment):
    from velune.memory.tiers.episodic import EpisodicMemory

    pool = env.container.get("runtime.sqlite_pool")
    return EpisodicMemory(pool)


def _create_three_brain_coordinator(env: RuntimeEnvironment):
    from velune.memory.three_brain import ThreeBrainCoordinator

    working = env.container.get("runtime.working_memory")
    semantic = env.container.get("runtime.semantic_memory_lance")
    episodic = env.container.get("runtime.episodic_session_memory")
    kg_query = (
        env.container.get("runtime.knowledge_query")
        if env.container.has("runtime.knowledge_query")
        else None
    )
    bus = env.container.get("runtime.bus") if env.container.has("runtime.bus") else None
    return ThreeBrainCoordinator(working, semantic, episodic, kg_query=kg_query, bus=bus)


def _create_memory_lifecycle(env: RuntimeEnvironment):
    from velune.memory.lifecycle import MemoryLifecycleManager

    working_tier = env.container.get("runtime.working_memory")
    episodic_tier = env.container.get("runtime.episodic_memory")
    episodic_memory = env.container.get("runtime.episodic_session_memory")
    semantic_memory = env.container.get("runtime.semantic_memory_lance")
    embedding_pipeline = env.container.get("runtime.embedding_pipeline")
    lineage_tier = env.container.get("runtime.lineage_memory")
    three_brain = env.container.get("runtime.three_brain_coordinator")
    provider_registry = (
        env.container.get("runtime.provider_registry")
        if env.container.has("runtime.provider_registry")
        else None
    )

    return MemoryLifecycleManager(
        working_tier=working_tier,
        episodic_memory=episodic_memory,
        semantic_memory=semantic_memory,
        embedding_pipeline=embedding_pipeline,
        lineage_tier=lineage_tier,
        episodic_session_memory=episodic_tier,
        three_brain=three_brain,
        provider_registry=provider_registry,
    )


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
        # Qdrant connection for code/repository vector search (used by
        # HybridRetriever) — not a conversational memory tier. Renamed from
        # "semantic_memory" alongside CodeVectorConnection (see
        # velune.memory.tiers.semantic) specifically so this can no longer be
        # mistaken for a second, competing implementation of
        # "runtime.semantic_memory_lance" (the real one, LanceDB-backed).
        name="code_vector_client",
        factory=_create_code_vector_connection,
        container_key="runtime.code_vector_client",
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
        name="three_brain_coordinator",
        factory=_create_three_brain_coordinator,
        container_key="runtime.three_brain_coordinator",
        lifecycle_key="three_brain_coordinator",
        dependencies=[
            "runtime.working_memory",
            "runtime.semantic_memory_lance",
            "runtime.episodic_session_memory",
            "runtime.knowledge_query",
        ],
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
            "runtime.three_brain_coordinator",
        ],
    ),
]
