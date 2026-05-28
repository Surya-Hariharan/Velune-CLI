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


@pytest.mark.asyncio
async def test_skip_embedding_when_provider_lacks_capability():
    """Embedding generation must be skipped cleanly when the provider lacks supports_embeddings capability."""
    from unittest.mock import MagicMock
    from velune.kernel.registry import get_container
    
    # 1. Setup mock provider without embedding support
    mock_provider = MagicMock()
    mock_caps = MagicMock()
    mock_caps.supports_embeddings = False
    mock_provider.get_capabilities.return_value = mock_caps
    
    mock_registry = MagicMock()
    mock_registry.get.return_value = mock_provider
    
    mock_config = MagicMock()
    mock_config.providers.default_provider = "mock-no-embeds"
    
    # 2. Setup mock container
    container = get_container()
    
    # Backup original components if present
    has_reg = container.has("runtime.provider_registry")
    has_conf = container.has("runtime.config")
    orig_reg = container.get("runtime.provider_registry") if has_reg else None
    orig_conf = container.get("runtime.config") if has_conf else None
    
    container.register_instance("runtime.provider_registry", mock_registry)
    container.register_instance("runtime.config", mock_config)
    
    try:
        retriever = HybridRetriever()
        emb = await retriever._generate_embedding_async("some text")
        
        # Verify it skipped and returned None
        assert emb is None
        mock_provider.get_capabilities.assert_called_once()
        mock_provider.embed.assert_not_called()
    finally:
        # Restore original components to avoid polluting subsequent tests
        if has_reg:
            container.register_instance("runtime.provider_registry", orig_reg)
        else:
            container._singletons.pop("runtime.provider_registry", None)
            
        if has_conf:
            container.register_instance("runtime.config", orig_conf)
        else:
            container._singletons.pop("runtime.config", None)
