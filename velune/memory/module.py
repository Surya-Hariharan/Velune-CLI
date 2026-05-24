from velune.kernel.bootstrap import SubsystemModule, RuntimeEnvironment
from pathlib import Path

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

def _create_archive_tier(env: RuntimeEnvironment):
    from velune.memory.tiers.archive import LongTermArchiveTier
    velune_dir = env.workspace / ".velune"
    velune_dir.mkdir(parents=True, exist_ok=True)
    archive_dir = velune_dir / "archive"
    return LongTermArchiveTier(archive_dir)

def _create_memory_consolidator(env: RuntimeEnvironment):
    from velune.memory.consolidator import MemoryConsolidator
    working_tier = env.container.get("runtime.working_memory")
    episodic_tier = env.container.get("runtime.episodic_memory")
    semantic_tier = env.container.get("runtime.semantic_memory")
    graph_tier = env.container.get("runtime.graph_memory")
    archive_tier = env.container.get("runtime.archive_memory")
    return MemoryConsolidator(
        working_tier=working_tier,
        episodic_tier=episodic_tier,
        semantic_tier=semantic_tier,
        graph_tier=graph_tier,
        archive_tier=archive_tier,
    )

def _create_memory_lifecycle(env: RuntimeEnvironment):
    from velune.memory.lifecycle import MemoryLifecycleCoordinator
    consolidator = env.container.get("runtime.memory_consolidator")
    return MemoryLifecycleCoordinator(consolidator)

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
        name="archive_memory",
        factory=_create_archive_tier,
        container_key="runtime.archive_memory",
    ),
    SubsystemModule(
        name="memory_consolidator",
        factory=_create_memory_consolidator,
        container_key="runtime.memory_consolidator",
        dependencies=[
            "runtime.working_memory",
            "runtime.episodic_memory",
            "runtime.semantic_memory",
            "runtime.graph_memory",
            "runtime.archive_memory",
        ],
    ),
    SubsystemModule(
        name="memory_lifecycle",
        factory=_create_memory_lifecycle,
        container_key="runtime.memory_lifecycle",
        lifecycle_key="memory",
        dependencies=["runtime.memory_consolidator"],
    ),
]
