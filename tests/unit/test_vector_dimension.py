import pytest
from qdrant_client import QdrantClient
from velune.retrieval.vector import VectorRetriever
from velune.retrieval.schemas import RetrievalDocument
from velune.memory.tiers.semantic import SemanticMemoryTier

def test_dimension_negotiation_768(tmp_path):
    """768-dim embeddings must create 768-dim collection."""
    client = QdrantClient(path=str(tmp_path / "qdrant"))
    retriever = VectorRetriever(collection_name="test", client=client)
    
    doc = RetrievalDocument(
        id="doc1", content="test",
        embedding=[0.1] * 768, namespace="default"
    )
    retriever.upsert(doc)
    
    assert retriever._detected_dimension == 768
    info = client.get_collection("test")
    assert info.config.params.vectors.size == 768

def test_dimension_mismatch_fails_gracefully(tmp_path):
    """Query with wrong dimension must return empty, not corrupt results."""
    client = QdrantClient(path=str(tmp_path / "qdrant"))
    retriever = VectorRetriever(collection_name="test", client=client)
    
    # Create with 768-dim
    retriever.upsert(RetrievalDocument(
        id="d1", content="hello", embedding=[0.1]*768, namespace="default"
    ))
    
    # Query with wrong dimension
    results = retriever.retrieve(query_vector=[0.1]*1536)
    assert results == []  # Must return empty, not crash

def test_no_zero_padding(tmp_path):
    """Verify that a 1536-dim vector is stored and queried at exactly 1536 dims with no padding distortion."""
    client = QdrantClient(path=str(tmp_path / "qdrant"))
    retriever = VectorRetriever(collection_name="test", client=client)
    
    doc = RetrievalDocument(
        id="d1", content="openai-style", embedding=[0.2]*1536, namespace="default"
    )
    retriever.upsert(doc)
    
    assert retriever._detected_dimension == 1536
    info = client.get_collection("test")
    assert info.config.params.vectors.size == 1536
    
    results = retriever.retrieve(query_vector=[0.2]*1536)
    assert len(results) == 1
    assert results[0].document.id == "d1"

def test_semantic_memory_tier_validation():
    tier = SemanticMemoryTier(location=":memory:")
    tier.create_collection("test-semantic", vector_size=768)
    
    # Uniform batch passes
    tier.upsert_points(
        collection_name="test-semantic",
        ids=["id1", "id2"],
        vectors=[[0.1]*768, [-0.1]*768],
        payloads=[{}, {}]
    )
    
    # Mixed dimensions batch raises ValueError
    with pytest.raises(ValueError) as excinfo:
        tier.upsert_points(
            collection_name="test-semantic",
            ids=["id1", "id2"],
            vectors=[[0.1]*768, [0.2]*1536],
            payloads=[{}, {}]
        )
    assert "Mixed embedding dimensions in batch" in str(excinfo.value)

    # Search similarity works with query_points
    results = tier.search_similarity(
        collection_name="test-semantic",
        query_vector=[0.1]*768,
        limit=1
    )
    assert len(results) == 1
    assert results[0]["id"] == tier._clean_id("id1")

