import pytest
import asyncio
from velune.providers.adapters.ollama import OllamaProvider
from velune.core.types.inference import InferenceRequest
from velune.providers.discovery.ollama import OllamaDiscovery

@pytest.mark.ollama
@pytest.mark.asyncio
async def test_ollama_model_discovery(skip_without_ollama, ollama_model):
    """Verify OllamaDiscovery correctly enumerates running models."""
    discovery = OllamaDiscovery()
    models = await discovery.discover()
    
    assert len(models) > 0
    model_ids = [m.model_id for m in models]
    assert ollama_model in model_ids
    
    # Verify model descriptor has expected fields
    model = next(m for m in models if m.model_id == ollama_model)
    assert model.provider_id == "ollama"
    assert model.context_length > 0
    assert model.is_local is True

@pytest.mark.ollama
@pytest.mark.asyncio
async def test_ollama_inference(skip_without_ollama, ollama_model):
    """Verify OllamaProvider can complete a simple inference."""
    provider = OllamaProvider()
    await provider.initialize()
    
    try:
        request = InferenceRequest(
            model_id=ollama_model,
            messages=[{"role": "user", "content": "Say exactly: hello"}],
            temperature=0.0,
            max_tokens=10,
        )
        response = await provider.infer(request)
        
        assert response.content
        assert len(response.content) > 0
        assert response.model_id == ollama_model
        assert response.finish_reason in ("stop", "length")
        assert response.latency_ms > 0
    finally:
        await provider.shutdown()

@pytest.mark.ollama
@pytest.mark.asyncio
async def test_ollama_streaming(skip_without_ollama, ollama_model):
    """Verify OllamaProvider streaming works correctly."""
    provider = OllamaProvider()
    await provider.initialize()
    
    try:
        request = InferenceRequest(
            model_id=ollama_model,
            messages=[{"role": "user", "content": "Count 1 2 3"}],
            temperature=0.0,
            max_tokens=20,
        )
        chunks = []
        async for chunk in provider.stream(request):
            chunks.append(chunk.content)
        
        assert len(chunks) > 0
        combined = "".join(chunks)
        assert len(combined) > 0
    finally:
        await provider.shutdown()

@pytest.mark.ollama
@pytest.mark.asyncio
async def test_ollama_health_check(skip_without_ollama):
    from velune.core.types.provider import ProviderHealth
    provider = OllamaProvider()
    await provider.initialize()
    
    health = await provider.health_check()
    assert health == ProviderHealth.HEALTHY
    await provider.shutdown()
