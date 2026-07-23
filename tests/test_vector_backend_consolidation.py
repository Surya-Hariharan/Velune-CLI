"""Consolidating the two vector-store implementations that used to both look
like "semantic memory": ``SemanticMemoryTier`` (Qdrant) was renamed to
``CodeVectorConnection`` and stripped of its unused tier-API — nothing
outside its own file ever called ``upsert_points``/``search_similarity``/
``delete_points``/``delete_by_payload``, only its lazy ``.client`` property
(handed to HybridRetriever for code/repository vector search). The real
conversational-memory vector store remains ``SemanticMemory`` (LanceDB),
unaffected — see docs/ARCHITECTURE.md §5 for the full reasoning.
"""

from __future__ import annotations

from velune.memory import CodeVectorConnection
from velune.memory.subsystems import MEMORY_MODULES
from velune.retrieval.subsystems import RETRIEVAL_MODULES


def test_code_vector_connection_has_no_leftover_tier_api():
    """The dead methods must actually be gone, not just unused."""
    for dead_name in (
        "upsert_points",
        "search_similarity",
        "delete_points",
        "delete_by_payload",
        "create_collection",
    ):
        assert not hasattr(CodeVectorConnection, dead_name), dead_name


def test_code_vector_connection_client_property_still_works():
    conn = CodeVectorConnection()
    conn._degraded = True  # avoid touching a real qdrant client in a unit test
    assert conn.client is None


def test_semantic_memory_tier_name_no_longer_exists():
    """The old, misleadingly-named class must not silently linger somewhere."""
    import velune.memory as m
    import velune.memory.tiers.semantic as sem

    assert not hasattr(m, "SemanticMemoryTier")
    assert not hasattr(sem, "SemanticMemoryTier")


def test_container_key_renamed_from_semantic_memory():
    keys = [mod.container_key for mod in MEMORY_MODULES]
    assert "runtime.code_vector_client" in keys
    assert "runtime.semantic_memory" not in keys
    # The real conversational-memory tier is untouched.
    assert "runtime.semantic_memory_lance" in keys


def test_hybrid_retriever_depends_on_renamed_key():
    retrieval_module = next(m for m in RETRIEVAL_MODULES if m.container_key == "runtime.retrieval")
    assert "runtime.code_vector_client" in retrieval_module.dependencies
    assert "runtime.semantic_memory" not in retrieval_module.dependencies
