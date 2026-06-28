"""Provider Lifecycle Manager."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from velune.core.errors.provider import ProviderAuthenticationError
from velune.core.types.provider import ProviderHealth
from velune.providers.base import ModelProvider
from velune.providers.registry import ProviderRegistry

logger = logging.getLogger("velune.providers.manager")


class ProviderManager:
    """Manages the lifecycle, health, and reconnection logic for all providers."""

    def __init__(self, registry: ProviderRegistry):
        self.registry = registry
        self._health_states: dict[str, ProviderHealth] = {}
        self._provider_locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, provider_id: str) -> asyncio.Lock:
        if provider_id not in self._provider_locks:
            self._provider_locks[provider_id] = asyncio.Lock()
        return self._provider_locks[provider_id]

    async def initialize_provider(self, provider_id: str) -> ProviderHealth:
        """Initialize a provider with exponential backoff on transient errors."""
        provider = self.registry.get_or_raise(provider_id)
        lock = self._get_lock(provider_id)

        async with lock:
            if getattr(provider, "client", None) is not None:
                # Already initialized
                return self._health_states.get(provider_id, ProviderHealth.UNKNOWN)

            retries = 3
            backoff = 1.0

            for attempt in range(retries):
                try:
                    await provider.initialize()
                    # Add authenticate step if it exists
                    if hasattr(provider, "authenticate"):
                        await provider.authenticate()
                    
                    health = await provider.health_check()
                    self._health_states[provider_id] = health
                    return health
                except ProviderAuthenticationError as e:
                    # Do not retry auth errors
                    logger.error("Authentication failed for %s: %s", provider_id, e)
                    self._health_states[provider_id] = ProviderHealth.UNAUTHORIZED
                    return ProviderHealth.UNAUTHORIZED
                except Exception as e:
                    logger.warning(
                        "Initialization attempt %d failed for %s: %s",
                        attempt + 1,
                        provider_id,
                        e,
                    )
                    if attempt == retries - 1:
                        self._health_states[provider_id] = ProviderHealth.OFFLINE
                        return ProviderHealth.OFFLINE
                    await asyncio.sleep(backoff)
                    backoff *= 2
            
            return ProviderHealth.OFFLINE

    async def get_health(self, provider_id: str) -> ProviderHealth:
        """Get the current health of a provider, initializing if necessary."""
        if provider_id not in self._health_states:
            return await self.initialize_provider(provider_id)
        return self._health_states[provider_id]

    async def check_all_health(self) -> dict[str, ProviderHealth]:
        """Concurrently check the health of all registered and available providers."""
        available = self.registry.list_available_providers()
        tasks = [self.initialize_provider(pid) for pid in available]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        health_map = {}
        for pid, res in zip(available, results):
            if isinstance(res, Exception):
                logger.error("Failed to check health for %s: %s", pid, res)
                health_map[pid] = ProviderHealth.OFFLINE
            else:
                health_map[pid] = res
        return health_map

    async def shutdown_all(self) -> None:
        """Shutdown all providers gracefully."""
        await self.registry.shutdown_all()
        self._health_states.clear()
