"""Unified model discovery coordinator."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

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
        "ollama",
        "lmstudio",
        "gguf",
        "llamacpp",
        "openai-compat",
        "vllm",
        "tgi",
        "localai",
        "docker",
        # "nvidia" is deliberately absent: NVIDIANIMDiscovery is hosted-only and
        # gated on a key like every other cloud provider. Self-hosted NIM
        # containers are OpenAI-compatible servers and come from
        # OpenAICompatDiscovery.
    }
)


@dataclass
class ScanReport:
    """What one discovery pass found, and how much of it can be trusted."""

    models: list[ModelDescriptor] = field(default_factory=list)
    #: Provider ids whose listing succeeded: their model set is authoritative.
    refreshed: set[str] = field(default_factory=set)
    #: Provider id -> why its listing failed (keep the last known models).
    failed: dict[str, str] = field(default_factory=dict)
    #: Discoverers not run (no key configured).
    skipped: set[str] = field(default_factory=set)


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
        from velune.providers.discovery.meta import MetaDiscovery
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
            DockerDiscovery(),  # Docker containers on local ports
            NVIDIANIMDiscovery(),  # NVIDIA NIM cloud + local containers
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
            MetaDiscovery(),
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
        from velune.providers.keystore import KeyState, has_key, verification_state

        if not has_key(discoverer.provider_id):
            return False
        # A key the provider has actively rejected cannot yield usable models —
        # listing them anyway lets the council seat roles that 401 on every
        # call. `/connect <provider>` in the REPL re-verifies and restores discovery.
        return verification_state(discoverer.provider_id) is not KeyState.INVALID

    async def _collect(self, discoverer) -> list[ModelDescriptor]:
        """Run a single discoverer, returning [] on any failure."""
        models, _error = await self._collect_with_status(discoverer)
        return models

    async def _collect_with_status(self, discoverer) -> tuple[list[ModelDescriptor], str | None]:
        """Run a discoverer; return its models and why it could not list, if it couldn't.

        A discoverer reports a failed listing either by raising or by setting
        ``last_error`` (the ones that fall back to a curated list do the latter,
        so the fallback is not mistaken for what the provider actually serves).
        """
        try:
            models = await discoverer.discover()
        except Exception as e:
            logger.debug("Discovery failed for %s: %s", discoverer.provider_id, e)
            return [], f"{type(e).__name__}: {e}"[:200]
        return models, getattr(discoverer, "last_error", None)

    @staticmethod
    def _covered_ids(discoverer, models: list[ModelDescriptor]) -> set[str]:
        """Provider ids whose model list this discoverer is the source for."""
        return (
            {discoverer.provider_id}
            | set(getattr(discoverer, "covers", ()))
            | {m.provider_id for m in models}
        )

    async def scan_all(self) -> list[ModelDescriptor]:
        """Scan all providers for models in parallel."""
        return (await self.scan_all_detailed()).models

    async def scan_all_detailed(self) -> ScanReport:
        """Scan every provider and report, per provider, whether listing worked.

        :class:`ScanReport` lets the registry tell "the provider no longer
        serves this model" (refreshed, model absent) from "we could not ask"
        (failed: offline, timeout, daemon down, drive unplugged). Only the
        former may remove a model; the latter keeps the last known list.
        """
        # Gate server-dependent local providers on reachability. Ollama is NOT
        # gated here: its discoverer also reads on-disk manifest stores, so it
        # must run even when the daemon is down (to surface models on external
        # drives or an offline daemon) — it returns [] cheaply if nothing exists.
        # Docker discovery returns [] cheaply when nothing is running, so it is
        # also not pre-checked.
        from velune.providers.discovery.lmstudio import LMStudioDiscovery
        from velune.providers.discovery.openai_compat import OpenAICompatDiscovery

        lmstudio_ok, openai_compat_ok = await asyncio.gather(
            LMStudioDiscovery.is_running(),
            OpenAICompatDiscovery.is_running(),
        )

        report = ScanReport()
        running = []
        for d in self.discoverers:
            if d.provider_id == "lmstudio" and not lmstudio_ok:
                report.failed["lmstudio"] = "LM Studio server is not running"
                continue
            if d.provider_id == "openai-compat" and not openai_compat_ok:
                for pid in self._covered_ids(d, []):
                    report.failed[pid] = "No OpenAI-compatible server is responding"
                continue
            if self._should_run(d):
                running.append(d)
            else:
                report.skipped.add(d.provider_id)

        results = await asyncio.gather(
            *(self._collect_with_status(d) for d in running), return_exceptions=True
        )

        all_models: list[ModelDescriptor] = []
        for d, result in zip(running, results, strict=True):
            if isinstance(result, BaseException):
                models, error = [], f"{type(result).__name__}: {result}"[:200]
            else:
                models, error = result
            all_models.extend(models)
            for pid in self._covered_ids(d, models):
                if error:
                    report.failed[pid] = error
                else:
                    report.refreshed.add(pid)
        report.models = all_models

        # Summary log
        counts: dict[str, int] = {
            "gguf": 0,
            "ollama": 0,
            "lmstudio": 0,
            "docker": 0,
            "nvidia": 0,
            "cloud": 0,
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
            counts["nvidia"],
            counts["cloud"],
        )
        if report.failed:
            logger.info(
                "Discovery could not list: %s",
                "; ".join(f"{pid} ({why})" for pid, why in sorted(report.failed.items())),
            )

        return report

    async def scan_provider(self, provider_id: str) -> list[ModelDescriptor]:
        """Scan a specific provider by ID."""
        return (await self.scan_provider_detailed(provider_id)).models

    async def scan_provider_detailed(self, provider_id: str) -> ScanReport:
        """Scan one provider, reporting whether its listing actually succeeded."""
        report = ScanReport()
        for discoverer in self.discoverers:
            if discoverer.provider_id == provider_id:
                models, error = await self._collect_with_status(discoverer)
                report.models = models
                for pid in self._covered_ids(discoverer, models):
                    if error:
                        report.failed[pid] = error
                    else:
                        report.refreshed.add(pid)
                return report
        report.skipped.add(provider_id)
        return report
