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

    def __init__(self, registry: ProviderRegistry, poll_interval: float = 300.0) -> None:
        self._registry = registry
        self._manifests: dict[str, CapabilityManifest] = {}
        self._latency_windows: dict[str, deque[int]] = defaultdict(lambda: deque(maxlen=5))
        self._health_history: dict[str, deque[ProviderHealth]] = defaultdict(
            lambda: deque(maxlen=3)
        )
        self._polling_task: asyncio.Task | None = None
        # Providers currently reported as "unavailable for 3 consecutive
        # polls" — logged once on entry into that state, not on every poll
        # while it persists. Cleared the moment health changes away from
        # UNAVAILABLE, so a real recovery-then-outage cycle warns again.
        self._flagged_unavailable: set[str] = set()
        # 5 minutes by default: this polls every *configured* provider,
        # including cloud ones, over the network for the life of the
        # process — a 30s interval (the original value) meant continuous
        # background API traffic against providers like Anthropic/OpenAI
        # purely for a health table nothing consumed. /doctor and
        # ProviderRouter's health filtering don't need second-level freshness.
        self._poll_interval = poll_interval
        self._health_check_timeout = 2.0  # seconds
        self._running = False

    async def initialize(self) -> None:
        """LifecycleCoordinator startup hook — delegates to :meth:`start`.

        Matches the ``hasattr(comp, "initialize")`` convention
        ``LifecycleCoordinator.startup()`` already uses for other DI-managed
        subsystems (see ``ThreeBrainCoordinator.initialize``), so registering
        this monitor with a ``lifecycle_key`` is enough to start it — no
        separate manual ``.start()`` call site is needed.
        """
        await self.start()

    async def shutdown(self) -> None:
        """LifecycleCoordinator shutdown hook — delegates to :meth:`stop`."""
        await self.stop()

    async def start(self) -> None:
        """Start the background polling task."""
        if self._running:
            logger.warning("ProviderHealthMonitor already running")
            return

        self._running = True
        self._polling_task = asyncio.create_task(self._polling_loop())
        logger.info("ProviderHealthMonitor started (polling every %.0fs)", self._poll_interval)

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

    def _requires_unset_key(self, provider_id: str) -> bool:
        """True if *provider_id* needs an API key and none is configured.

        Sourced from the provider catalog (the same "requires_key" fact
        ``/providers``, ``/connect``, and the setup wizard use) rather than
        guessing from the adapter's behavior at call time.
        """
        from velune.providers import catalog

        meta = catalog.get(provider_id)
        if meta is None or not meta.requires_key:
            return False
        return not self._registry.check_provider_available(provider_id)

    def _record_unconfigured(self, provider_id: str) -> None:
        """Record UNCONFIGURED without a live probe, and without repolling it."""
        self._flagged_unavailable.discard(provider_id)
        self._health_history[provider_id].clear()
        existing = self._manifests.get(provider_id)
        if existing is not None and existing.health == ProviderHealth.UNCONFIGURED:
            return
        self._manifests[provider_id] = CapabilityManifest(
            provider_id=provider_id,
            health=ProviderHealth.UNCONFIGURED,
            available_models=[],
            is_online=False,
            refreshed_at=time.time(),
        )

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
                # Get all registered provider IDs. A key-requiring provider
                # with no credentials configured is UNCONFIGURED, not
                # UNAVAILABLE — it has never been set up, so there is nothing
                # to poll and nothing wrong to warn about. Only providers that
                # are actually configured (local, or a key is present) get a
                # live health_check.
                providers_to_check = []
                for provider_id in standard_providers:
                    if not self._registry.get(provider_id):
                        continue
                    if self._requires_unset_key(provider_id):
                        self._record_unconfigured(provider_id)
                        continue
                    provider = self._registry.get(provider_id)
                    if provider:
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
        except (asyncio.TimeoutError, TimeoutError):  # distinct classes before 3.11
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

        # 3 consecutive unavailable polls: a real, configured provider that
        # has gone unreachable is worth one state-transition warning — logged
        # once on entry into that state, never repeated every poll while it
        # persists (that produced an unbroken stream of identical warnings
        # for the life of the process). Cleared as soon as health improves so
        # a later, genuinely new outage warns again.
        if health != ProviderHealth.UNAVAILABLE:
            self._flagged_unavailable.discard(provider_id)
        elif len(self._health_history[provider_id]) >= 3:
            recent = list(self._health_history[provider_id])
            if all(h == ProviderHealth.UNAVAILABLE for h in recent[-3:]):
                if provider_id not in self._flagged_unavailable:
                    self._flagged_unavailable.add(provider_id)
                    logger.warning(f"Provider {provider_id} unavailable for 3 consecutive polls")

        self._manifests[provider_id] = manifest
