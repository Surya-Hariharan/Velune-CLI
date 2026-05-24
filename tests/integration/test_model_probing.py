import pytest
import asyncio

@pytest.mark.ollama
@pytest.mark.slow
@pytest.mark.asyncio
async def test_coding_probe_on_real_model(skip_without_ollama, ollama_model):
    """Verify ModelProber returns sensible scores for real models."""
    from velune.models.probes import ModelProber
    from velune.providers.adapters.ollama import OllamaProvider
    
    provider = OllamaProvider()
    await provider.initialize()
    
    try:
        prober = ModelProber(provider, ollama_model)
        result = await prober.run_coding_probe()
        
        # Don't assert specific scores (varies by model) but verify structure
        assert 0.0 <= result.score <= 1.0
        assert result.latency_ms > 0
        assert result.capability == "coding"
    finally:
        await provider.shutdown()
