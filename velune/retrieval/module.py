from velune.kernel.bootstrap import RuntimeEnvironment, SubsystemModule


def _create_hybrid_retriever(env: RuntimeEnvironment):
    from velune.core.paths import qdrant_store_path
    from velune.retrieval.hybrid import HybridRetriever

    vector_path = str(qdrant_store_path(env.workspace))
    semantic_tier = env.container.get("runtime.semantic_memory")
    # Share the semantic tier's single Qdrant connection, but resolve it lazily
    # via a provider so wiring retrieval at bootstrap does not open the store.
    return HybridRetriever(
        location=vector_path,
        client_provider=lambda: semantic_tier.client,
    )


RETRIEVAL_MODULES = [
    SubsystemModule(
        name="retrieval",
        factory=_create_hybrid_retriever,
        container_key="runtime.retrieval",
        lifecycle_key="retrieval",
        dependencies=["runtime.semantic_memory"],
    )
]
