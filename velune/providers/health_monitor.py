"""Provider health monitoring with real-time capability tracking."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque

from velune.core.types.provider import CapabilityManifest, ProviderHealth
from velune.providers.base import ModelProvider
from velune.providers.registry import ProviderRegistry

logger = logging.getLogger("velune.providers.health_monitor")


class ProviderHealthMonitor:
    """Continuously polls registered providers and maintains real-time capability manifests."""

    def __init__(self, registry: ProviderRegistry) -> None:
        self._registry = registry
        self._manifests: dict[str, CapabilityManifest] = {}
        self._latency_windows: dict[str, deque[int]] = defaultdict(lambda: deque(maxlen=5))
        self._health_history: dict[str, deque[ProviderHealth]] = defaultdict(
            lambda: deque(maxlen=3)
        )
        self._polling_task: asyncio.Task | None = None
        self._poll_interval = 30.0  # seconds
        self._health_check_timeout = 2.0  # seconds
        self._running = False

    async def start(self) -> None:
        """Start the background polling task."""
        if self._running:
            logger.warning("ProviderHealthMonitor already running")
            return

        self._running = True
        self._polling_task = asyncio.create_task(self._polling_loop())
        logger.info("ProviderHealthMonitor started")

    async def stop(self) -> None:
        """Stop the background polling task."""
        self._running = False
        if self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
        logger.info("ProviderHealthMonitor stopped")

    def get_manifest(self, provider_id: str) -> CapabilityManifest | None:
        """Get the latest manifest for a provider."""
        return self._manifests.get(provider_id)

    def get_all_manifests(self) -> dict[str, CapabilityManifest]:
        """Get all provider manifests."""
        return self._manifests.copy()

    def record_latency(self, provider_id: str, latency_ms: int) -> None:
        """Record a call latency for rolling average calculation."""
        self._latency_windows[provider_id].append(latency_ms)
        if manifest := self._manifests.get(provider_id):
            avg_latency = int(
                sum(self._latency_windows[provider_id]) / len(self._latency_windows[provider_id])
            )
            manifest.estimated_latency_ms = avg_latency

    async def _polling_loop(self) -> None:
        """Background task that polls all providers every 30 seconds."""
        # Standard provider IDs that may be registered
        standard_providers = [
            "ollama",
            "openai",
            "anthropic",
            "google",
            "groq",
            "xai",
            "openrouter",
            "together",
            "fireworks",
            "huggingface",
            "lmstudio",
            "llamacpp",
            "deepseek",
            "mistral",
            "cohere",
            "nvidia",
            "meta",
        ]

        while self._running:
            try:
                # Get all registered provider IDs
                providers_to_check = []
                for provider_id in standard_providers:
                    if provider := self._registry.get(provider_id):
                        providers_to_check.append((provider_id, provider))

                # Poll all providers in parallel
                tasks = [
                    self._health_check_provider(provider_id, provider)
                    for provider_id, provider in providers_to_check
                ]
                await asyncio.gather(*tasks, return_exceptions=True)

            except Exception as e:
                logger.error(f"Error in health monitoring loop: {e}")

            # Sleep before next poll
            try:
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                break

    async def _health_check_provider(self, provider_id: str, provider: ModelProvider) -> None:
        """Check health of a single provider and update manifest."""
        try:
            # Call health_check with timeout
            health = await asyncio.wait_for(
                provider.health_check(), timeout=self._health_check_timeout
            )
        except TimeoutError:
            health = ProviderHealth.DEGRADED
            logger.debug(f"Provider {provider_id} health check timed out")
        except Exception as e:
            health = ProviderHealth.UNAVAILABLE
            logger.debug(f"Provider {provider_id} health check failed: {e}")

        try:
            # Get list of available models
            available_models = await asyncio.wait_for(
                provider.list_models(), timeout=self._health_check_timeout
            )
        except (TimeoutError, Exception):
            available_models = []

        # Get provider capabilities
        capabilities = provider.get_capabilities()

        # Track health history for consecutive unavailability detection
        self._health_history[provider_id].append(health)

        # Create or update manifest
        manifest = CapabilityManifest(
            provider_id=provider_id,
            health=health,
            available_models=available_models,
            rate_limit_remaining=None,  # Would be populated from response headers
            rate_limit_reset_at=None,
            estimated_latency_ms=int(
                sum(self._latency_windows[provider_id]) / len(self._latency_windows[provider_id])
            )
            if self._latency_windows[provider_id]
            else 0,
            supports_streaming=capabilities.supports_streaming,
            supports_tools=capabilities.supports_function_calling,
            is_online=True,  # Connected check would be done here
            refreshed_at=time.time(),
        )

        # Detect status changes
        old_manifest = self._manifests.get(provider_id)
        if old_manifest and old_manifest.health != health:
            logger.info(f"Provider {provider_id} health changed: {old_manifest.health} → {health}")

        # Check for 3 consecutive unavailable polls
        if len(self._health_history[provider_id]) >= 3:
            recent = list(self._health_history[provider_id])
            if all(h == ProviderHealth.UNAVAILABLE for h in recent[-3:]):
                logger.warning(f"Provider {provider_id} unavailable for 3 consecutive polls")

        self._manifests[provider_id] = manifest
