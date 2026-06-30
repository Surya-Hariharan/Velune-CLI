"""Unified model discovery coordinator."""

from __future__ import annotations

import asyncio
import logging

from velune.core.types.model import ModelDescriptor

# The concrete discovery classes are imported lazily in ``_build_discoverers``
# (and where used) so that merely importing this module — which happens during
# the Tier-0 model registry bootstrap — does not pull in ``httpx`` × 13 + every
# discovery backend.

logger = logging.getLogger("velune.providers.discovery.scanner")

# Providers that run locally and need no API key.
# Sub-types produced by the enhanced openai_compat fingerprinting are included
# so the _should_run() check doesn't gate them on a keystore lookup.
_LOCAL_PROVIDERS: frozenset[str] = frozenset(
    {
        "ollama", "lmstudio", "gguf", "llamacpp",
        "openai-compat", "vllm", "tgi", "localai",
        "docker", "nvidia_nim_local",
    }
)


class ModelDiscoveryScanner:
    """Coordinates model discovery across all configured providers.

    Cloud discoverers are only invoked when a key is available for that
    provider.  Local discoverers always run; Ollama and LM Studio are
    additionally gated on their daemon being reachable.
    """

    def __init__(self) -> None:
        # Discoverers are built lazily on first scan. Constructing them eagerly
        # imports ``httpx`` × 13 and runs a keystore ``get_key()`` lookup plus an
        # Ollama ``GPUDetector()`` per discoverer — ~0.4s that every Tier-0
        # bootstrap (and the first REPL prompt) paid for work nothing on the
        # startup path actually uses. Nothing scans until a discovery command
        # runs, so defer it.
        self._discoverers: list | None = None

    @staticmethod
    def _build_discoverers() -> list:
        """Import and instantiate every discovery backend (first-use only)."""
        from velune.providers.discovery.anthropic import AnthropicDiscovery
        from velune.providers.discovery.docker import DockerDiscovery
        from velune.providers.discovery.fireworks import FireworksDiscovery
        from velune.providers.discovery.gguf import GGUFDiscovery
        from velune.providers.discovery.google import GoogleDiscovery
        from velune.providers.discovery.groq import GroqDiscovery
        from velune.providers.discovery.huggingface import HuggingFaceDiscovery
        from velune.providers.discovery.lmstudio import LMStudioDiscovery
        from velune.providers.discovery.nvidia_nim import NVIDIANIMDiscovery
        from velune.providers.discovery.ollama import OllamaDiscovery
        from velune.providers.discovery.openai import OpenAIDiscovery
        from velune.providers.discovery.openai_compat import OpenAICompatDiscovery
        from velune.providers.discovery.openrouter import OpenRouterDiscovery
        from velune.providers.discovery.together import TogetherDiscovery
        from velune.providers.discovery.xai import XAIDiscovery

        return [
            OllamaDiscovery(),
            LMStudioDiscovery(),
            OpenAICompatDiscovery(),
            DockerDiscovery(),       # Docker containers on local ports
            NVIDIANIMDiscovery(),    # NVIDIA NIM cloud + local containers
            GGUFDiscovery(),
            HuggingFaceDiscovery(),
            OpenAIDiscovery(),
            AnthropicDiscovery(),
            XAIDiscovery(),
            GoogleDiscovery(),
            GroqDiscovery(),
            OpenRouterDiscovery(),
            TogetherDiscovery(),
            FireworksDiscovery(),
        ]

    @property
    def discoverers(self) -> list:
        """The discovery backends, built on first access and cached."""
        if self._discoverers is None:
            self._discoverers = self._build_discoverers()
        return self._discoverers

    def _should_run(self, discoverer) -> bool:
        """Return True if this discoverer should be queried."""
        if discoverer.provider_id in _LOCAL_PROVIDERS:
            return True
        from velune.providers.keystore import has_key

        return has_key(discoverer.provider_id)

    async def _collect(self, discoverer) -> list[ModelDescriptor]:
        """Run a single discoverer, returning [] on any failure."""
        try:
            return await discoverer.discover()
        except Exception as e:
            logger.debug("Discovery failed for %s: %s", discoverer.provider_id, e)
            return []

    async def scan_all(self) -> list[ModelDescriptor]:
        """Scan all providers for models in parallel."""
        # Gate server-dependent local providers on reachability. Ollama is NOT
        # gated here: its discoverer also reads on-disk manifest stores, so it
        # must run even when the daemon is down (to surface models on external
        # drives or an offline daemon) — it returns [] cheaply if nothing exists.
        # Docker and NIM discoverers return [] cheaply when nothing is running,
        # so they are also not pre-checked.
        from velune.providers.discovery.lmstudio import LMStudioDiscovery
        from velune.providers.discovery.openai_compat import OpenAICompatDiscovery

        lmstudio_ok, openai_compat_ok = await asyncio.gather(
            LMStudioDiscovery.is_running(),
            OpenAICompatDiscovery.is_running(),
        )

        tasks = []
        for d in self.discoverers:
            if d.provider_id == "lmstudio" and not lmstudio_ok:
                continue
            if d.provider_id == "openai-compat" and not openai_compat_ok:
                continue
            if self._should_run(d):
                tasks.append(self._collect(d))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_models: list[ModelDescriptor] = []
        for result in results:
            if isinstance(result, list):
                all_models.extend(result)

        # Summary log
        counts: dict[str, int] = {
            "gguf": 0, "ollama": 0, "lmstudio": 0,
            "docker": 0, "nvidia_nim": 0, "cloud": 0,
        }
        for m in all_models:
            if m.provider_id in counts:
                counts[m.provider_id] += 1
            elif m.provider_id in _LOCAL_PROVIDERS:
                pass  # local sub-type (vllm/tgi/localai)
            else:
                counts["cloud"] += 1

        logger.info(
            "Local: %d GGUF, %d Ollama, %d LM Studio, %d Docker | NIM: %d | Cloud: %d models",
            counts["gguf"],
            counts["ollama"],
            counts["lmstudio"],
            counts["docker"],
            counts["nvidia_nim"],
            counts["cloud"],
        )

        return all_models

    async def scan_provider(self, provider_id: str) -> list[ModelDescriptor]:
        """Scan a specific provider by ID."""
        for discoverer in self.discoverers:
            if discoverer.provider_id == provider_id:
                return await self._collect(discoverer)
        return []
