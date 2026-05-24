from velune.kernel.bootstrap import SubsystemModule, RuntimeEnvironment

def _create_hybrid_retriever(env: RuntimeEnvironment):
    from velune.retrieval.hybrid import HybridRetriever
    velune_dir = env.workspace / ".velune"
    vector_path = str(velune_dir / "qdrant_local_store")
    semantic_tier = env.container.get("runtime.semantic_memory")
    return HybridRetriever(location=vector_path, client=semantic_tier.client)

RETRIEVAL_MODULES = [
    SubsystemModule(
        name="retrieval",
        factory=_create_hybrid_retriever,
        container_key="runtime.retrieval",
        lifecycle_key="retrieval",
        dependencies=["runtime.semantic_memory"],
    )
]
