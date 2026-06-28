import asyncio
import pytest
from unittest.mock import patch, MagicMock

from velune.providers.adapters.nvidia import NVIDIAProvider
from velune.core.errors.provider import ProviderAuthenticationError, ProviderConnectionError
from velune.core.types.provider import ProviderHealth

class MockResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data
        
    def json(self):
        return self._json
        
    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("Error", request=MagicMock(), response=self)

@pytest.mark.asyncio
async def test_nvidia_authenticate_success():
    provider = NVIDIAProvider(api_key="valid")
    
    async def mock_get(self, url, *args, **kwargs):
        return MockResponse(200, {"data": []})
        
    with patch("httpx.AsyncClient.get", new=mock_get):
        await provider.authenticate()

@pytest.mark.asyncio
async def test_nvidia_authenticate_failure():
    provider = NVIDIAProvider(api_key="invalid")
    
    async def mock_get(self, url, *args, **kwargs):
        return MockResponse(401, {})
        
    with patch("httpx.AsyncClient.get", new=mock_get):
        with pytest.raises(ProviderAuthenticationError, match="invalid or expired"):
            await provider.authenticate()

@pytest.mark.asyncio
async def test_nvidia_dynamic_discovery():
    provider = NVIDIAProvider(api_key="valid")
    
    async def mock_get(self, url, *args, **kwargs):
        if url == "/models":
            return MockResponse(200, {
                "data": [
                    {"id": "meta/llama-3.1-70b-instruct"},
                    {"id": "nv-embedqa-e5-v5", "type": "embedding"}
                ]
            })
        return MockResponse(404, {})
        
    with patch("httpx.AsyncClient.get", new=mock_get):
        models = await provider.list_models()
        assert len(models) == 2
        
        # Verify it picks up the correct models
        model_ids = [m.model_id for m in models]
        assert "meta/llama-3.1-70b-instruct" in model_ids
        assert "nv-embedqa-e5-v5" in model_ids
        
        # Check context length assignment heuristics
        llama = next(m for m in models if m.model_id == "meta/llama-3.1-70b-instruct")
        assert llama.context_length >= 128000
