"""Model capability registry with empirical probe evaluation."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor
from velune.models.profile_cache import ModelProfileCache
from velune.providers.discovery.scanner import ModelDiscoveryScanner

logger = logging.getLogger("velune.models.registry")


class ModelCapabilityRegistry:
    """Unified database cataloging discovered models and capabilities with empirical routing."""

    def __init__(self, scanner: ModelDiscoveryScanner | None = None) -> None:
        self.scanner = scanner or ModelDiscoveryScanner()
        self._models: dict[str, ModelDescriptor] = {}

    async def refresh(self) -> None:
        """Scan all providers and refresh the local catalog cache with empirical profiles."""
        try:
            discovered = await self.scanner.scan_all()
            self._models.clear()

            profile_cache = ModelProfileCache(Path(".velune") / "model_profiles.json")
            from velune.models.probes import FastProbe

            fast_probe = FastProbe()

            probing_tasks = []
            models_to_probe = []

            for model in discovered:
                cached = profile_cache.get(model.model_id, model.provider_id)
                if cached:
                    # Apply cached probe results to capability profile
                    self._apply_probe_results(model, cached["probes"])
                else:
                    # Check if provider is available to probe
                    provider = None
                    try:
                        from velune.kernel.registry import get_container

                        provider_reg = get_container().get("runtime.provider_registry")
                        provider = provider_reg.get(model.provider_id)
                    except Exception:
                        pass

                    if provider:
                        models_to_probe.append(model)
                        probing_tasks.append(fast_probe.ping(provider, model.model_id))

            # Execute fast probes concurrently
            if probing_tasks:
                results = await asyncio.gather(*probing_tasks, return_exceptions=True)
                for model, responsive in zip(models_to_probe, results):
                    if isinstance(responsive, Exception):
                        responsive = False

                    if responsive:
                        model.metadata["validated"] = True
                        try:
                            from velune.daemon.client import DaemonClient

                            if DaemonClient.is_running():
                                # Delegate background probing to the active persistent Velune daemon!
                                # Using create_task to fire-and-forget the IPC dispatch call
                                asyncio.create_task(
                                    DaemonClient.send_command(
                                        "probe_model",
                                        model_id=model.model_id,
                                        provider_id=model.provider_id,
                                    )
                                )
                                logger.info(
                                    "Delegated full probing of model %s to the active Velune daemon process.",
                                    model.model_id,
                                )
                            else:
                                from velune.kernel.registry import get_container

                                task_reg = get_container().get("runtime.task_registry")
                                task_reg.submit(
                                    name=f"full_probe_{model.model_id}",
                                    coro=self._probe_model_background(model, profile_cache),
                                    timeout_seconds=120.0,
                                )
                        except Exception:
                            pass
                    else:
                        model.metadata["validated"] = False
                        logger.info("Model %s is not responding, skipping probe", model.model_id)

            # Store models in mapping
            for model in discovered:
                key = f"{model.provider_id}/{model.model_id}"
                self._models[key] = model
                if model.model_id not in self._models:
                    self._models[model.model_id] = model

            logger.info(
                "Indexed %d models (%d validated)",
                len(discovered),
                sum(1 for m in discovered if m.metadata.get("validated", True)),
            )
        except Exception as e:
            logger.error("Failed to discover models during catalog refresh: %s", e)

    def register(self, descriptor: ModelDescriptor) -> None:
        """Explicitly register a custom model descriptor."""
        key = f"{descriptor.provider_id}/{descriptor.model_id}"
        self._models[key] = descriptor
        if descriptor.model_id not in self._models:
            self._models[descriptor.model_id] = descriptor

    def get(self, model_id: str, provider_id: str | None = None) -> ModelDescriptor | None:
        """Look up a model descriptor by ID and optional provider prefix."""
        if provider_id:
            key = f"{provider_id}/{model_id}"
            return self._models.get(key)

        # Try direct match
        if model_id in self._models:
            return self._models[model_id]

        # Try searching values
        for model in self._models.values():
            if model.model_id == model_id:
                return model
        return None

    def list_all(self) -> list[ModelDescriptor]:
        """List all currently indexed model descriptors."""
        # Return unique descriptors
        seen = set()
        unique = []
        for model in self._models.values():
            ref = (model.provider_id, model.model_id)
            if ref not in seen:
                seen.add(ref)
                unique.append(model)
        return unique

    def get_by_provider(self, provider_id: str) -> list[ModelDescriptor]:
        """List all models registered under a specific provider."""
        return [model for model in self.list_all() if model.provider_id == provider_id]

    def _apply_probe_results(self, model: ModelDescriptor, probes: dict) -> None:
        """Map float probe scores (0.0-1.0) to CapabilityLevel and update model descriptor.

        Score mapping (empirical calibration):
        - score > 0.85 → EXPERT (100)
        - score > 0.70 → ADVANCED (75)
        - score > 0.50 → INTERMEDIATE (50)
        - else → BASIC (25)
        """
        if not model.capabilities:
            model.capabilities = ModelCapabilityProfile()

        def score_to_level(score: float) -> CapabilityLevel:
            if score > 0.85:
                return CapabilityLevel.EXPERT
            elif score > 0.70:
                return CapabilityLevel.ADVANCED
            elif score > 0.50:
                return CapabilityLevel.INTERMEDIATE
            elif score > 0.0:
                return CapabilityLevel.BASIC
            return CapabilityLevel.NONE

        coding_data = probes.get("coding", {})
        coding_score = (
            coding_data.score if hasattr(coding_data, "score") else coding_data.get("score", 0.0)
        )

        reasoning_data = probes.get("reasoning", {})
        reasoning_score = (
            reasoning_data.score
            if hasattr(reasoning_data, "score")
            else reasoning_data.get("score", 0.0)
        )

        instruction_data = probes.get("instruction", {})
        instruction_score = (
            instruction_data.score
            if hasattr(instruction_data, "score")
            else instruction_data.get("score", 0.0)
        )

        model.capabilities.coding = score_to_level(coding_score)
        model.capabilities.reasoning = score_to_level(reasoning_score)
        model.capabilities.instruction_following = score_to_level(instruction_score)

        # Infer other capabilities from primary scores
        if model.capabilities.reasoning >= CapabilityLevel.INTERMEDIATE:
            model.capabilities.planning = CapabilityLevel.INTERMEDIATE
        if model.capabilities.instruction_following >= CapabilityLevel.INTERMEDIATE:
            model.capabilities.tool_use = CapabilityLevel.INTERMEDIATE
        if model.capabilities.coding >= CapabilityLevel.INTERMEDIATE:
            model.capabilities.code_analysis = CapabilityLevel.INTERMEDIATE

        logger.debug(
            "Applied probe results to %s: coding=%s (%.2f), reasoning=%s (%.2f), instruction=%s (%.2f)",
            model.model_id,
            model.capabilities.coding.name,
            coding_score,
            model.capabilities.reasoning.name,
            reasoning_score,
            model.capabilities.instruction_following.name,
            instruction_score,
        )

    async def _probe_model_background(
        self, model: ModelDescriptor, cache: ModelProfileCache
    ) -> None:
        """Run probes in background, update model in registry when done."""
        try:
            from velune.kernel.registry import get_container

            container = get_container()
            if not container.has("runtime.provider_registry"):
                logger.debug(
                    "No provider registry registered yet, skipping background probe for %s.",
                    model.model_id,
                )
                return

            provider_registry = container.get("runtime.provider_registry")
            provider = provider_registry.get(model.provider_id)
            if not provider:
                logger.debug("No active provider found for %s, skipping probe.", model.model_id)
                return

            from velune.models.probes import ModelProber

            prober = ModelProber(provider, model.model_id)
            results = await prober.run_all_probes()
            cache.set(model.model_id, model.provider_id, results)
            self._apply_probe_results(model, results)
            logger.info(
                "Successfully probed %s: coding=%.2f reasoning=%.2f",
                model.model_id,
                results["coding"].score,
                results["reasoning"].score,
            )
        except Exception as e:
            logger.debug("Background probe failed for %s: %s", model.model_id, e)
