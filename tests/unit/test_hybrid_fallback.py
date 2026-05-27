import pytest
import os
from velune.retrieval.hybrid import HybridRetriever
from velune.retrieval.schemas import RetrievalQuery, RetrievalSource


@pytest.mark.asyncio
async def test_no_fallback_by_default():
    """Vector retrieval must be skipped when no provider, not use garbage."""
    retriever = HybridRetriever()
    # No provider registered
    
    query = RetrievalQuery(text="find auth code", top_k=5)
    result = await retriever.retrieve(query)
    
    # Must return results (from BM25 only), no vector hits with garbage embedding
    for hit in result.hits:
        assert hit.source != RetrievalSource.VECTOR


@pytest.mark.asyncio
async def test_fallback_enabled_via_env(monkeypatch):
    """Fallback embedding must work when explicitly enabled."""
    monkeypatch.setenv("VELUNE_ALLOW_FALLBACK_EMBEDDING", "true")
    retriever = HybridRetriever()
    emb = await retriever._generate_embedding_async("test text")
    assert emb is not None
    assert len(emb) == 1536


@pytest.mark.asyncio
async def test_check_embedding_available():
    """check_embedding_available must return False when no provider is available and fallback is disabled."""
    retriever = HybridRetriever()
    assert await retriever.check_embedding_available() is False


@pytest.mark.asyncio
async def test_check_embedding_available_with_fallback(monkeypatch):
    """check_embedding_available must return True when fallback is enabled."""
    monkeypatch.setenv("VELUNE_ALLOW_FALLBACK_EMBEDDING", "true")
    retriever = HybridRetriever()
    assert await retriever.check_embedding_available() is True
