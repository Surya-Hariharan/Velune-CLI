from unittest.mock import AsyncMock, patch

import pytest

from velune.core.errors.provider import ProviderAuthenticationError
from velune.core.types.provider import ProviderHealth
from velune.providers.base import ModelProvider
from velune.providers.manager import ProviderManager
from velune.providers.registry import ProviderRegistry


class MockFailingProvider(ModelProvider):
    def __init__(self, failure_type="transient", max_failures=2):
        self.failure_type = failure_type
        self.max_failures = max_failures
        self.failures = 0
        self.client = None

    @property
    def provider_id(self) -> str:
        return "mock"

    async def initialize(self) -> None:
        self.failures += 1
        if self.failures <= self.max_failures:
            if self.failure_type == "auth":
                raise ProviderAuthenticationError("Invalid key")
            else:
                raise Exception("Transient network error")
        self.client = True  # Mark initialized

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth.HEALTHY

    async def list_models(self) -> list:
        return []

    async def infer(self, request):
        pass

    async def stream(self, request):
        pass

    async def embed(self, texts, model_id):
        pass

    def get_capabilities(self):
        return None

    async def shutdown(self):
        pass


@pytest.mark.asyncio
async def test_provider_manager_transient_recovery():
    registry = ProviderRegistry()
    provider = MockFailingProvider(failure_type="transient", max_failures=2)
    registry.register("mock", provider)

    manager = ProviderManager(registry)

    # Fast backoff for test
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        health = await manager.initialize_provider("mock")

        assert health == ProviderHealth.HEALTHY
        assert provider.failures == 3  # Failed twice, succeeded on third
        assert mock_sleep.call_count == 2


@pytest.mark.asyncio
async def test_provider_manager_auth_failure():
    registry = ProviderRegistry()
    provider = MockFailingProvider(failure_type="auth", max_failures=2)
    registry.register("mock", provider)

    manager = ProviderManager(registry)

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        health = await manager.initialize_provider("mock")

        assert health == ProviderHealth.UNAUTHORIZED
        assert provider.failures == 1  # Should not retry auth failures
        assert mock_sleep.call_count == 0


@pytest.mark.asyncio
async def test_provider_manager_offline_failure():
    registry = ProviderRegistry()
    # Fails 5 times, which is > max retries (3)
    provider = MockFailingProvider(failure_type="transient", max_failures=5)
    registry.register("mock", provider)

    manager = ProviderManager(registry)

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        health = await manager.initialize_provider("mock")

        assert health == ProviderHealth.OFFLINE
        assert provider.failures == 3  # Failed all 3 attempts
        assert mock_sleep.call_count == 2
