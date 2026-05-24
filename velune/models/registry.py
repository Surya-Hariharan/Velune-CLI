"""Model capability registry with empirical probe evaluation."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Dict, List, Optional

from velune.core.types.model import ModelDescriptor, ModelCapabilityProfile, CapabilityLevel
from velune.providers.discovery.scanner import ModelDiscoveryScanner
from velune.models.profile_cache import ModelProfileCache

logger = logging.getLogger("velune.models.registry")


class ModelCapabilityRegistry:
    """Unified database cataloging discovered models and capabilities with empirical routing."""

    def __init__(self, scanner: Optional[ModelDiscoveryScanner] = None) -> None:
        self.scanner = scanner or ModelDiscoveryScanner()
        self._models: Dict[str, ModelDescriptor] = {}

    async def refresh(self) -> None:
        """Scan all providers and refresh the local catalog cache with empirical profiles."""
        try:
            discovered = await self.scanner.scan_all()
            self._models.clear()
            
            profile_cache = ModelProfileCache(Path(".velune") / "model_profiles.json")

            for model in discovered:
                cached = profile_cache.get(model.model_id, model.provider_id)
                if cached:
                    # Apply cached probe results to capability profile
                    self._apply_probe_results(model, cached["probes"])
                else:
                    # Schedule background probing (don't block refresh)
                    asyncio.create_task(self._probe_model_background(model, profile_cache))

                # Store under a fully qualified key: provider_id/model_id
                key = f"{model.provider_id}/{model.model_id}"
                self._models[key] = model
                # Also store under simple model_id if not already present
                if model.model_id not in self._models:
                    self._models[model.model_id] = model

            logger.info("Successfully indexed %d active models with empirical capability profiles.", len(discovered))
        except Exception as e:
            logger.error("Failed to discover models during catalog refresh: %s", e)

    def register(self, descriptor: ModelDescriptor) -> None:
        """Explicitly register a custom model descriptor."""
        key = f"{descriptor.provider_id}/{descriptor.model_id}"
        self._models[key] = descriptor
        if descriptor.model_id not in self._models:
            self._models[descriptor.model_id] = descriptor

    def get(self, model_id: str, provider_id: Optional[str] = None) -> Optional[ModelDescriptor]:
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

    def list_all(self) -> List[ModelDescriptor]:
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

    def get_by_provider(self, provider_id: str) -> List[ModelDescriptor]:
        """List all models registered under a specific provider."""
        return [model for model in self.list_all() if model.provider_id == provider_id]

    def _apply_probe_results(self, model: ModelDescriptor, probes: dict) -> None:
        """Map float probe scores to CapabilityLevel and update model descriptor."""
        if not model.capabilities:
            model.capabilities = ModelCapabilityProfile()
            
        def score_to_level(score: float) -> CapabilityLevel:
            if score >= 0.9:
                return CapabilityLevel.EXPERT
            elif score >= 0.7:
                return CapabilityLevel.ADVANCED
            elif score >= 0.4:
                return CapabilityLevel.INTERMEDIATE
            elif score >= 0.1:
                return CapabilityLevel.BASIC
            return CapabilityLevel.NONE

        coding_data = probes.get("coding", {})
        coding_score = coding_data.score if hasattr(coding_data, "score") else coding_data.get("score", 0.0)
        
        reasoning_data = probes.get("reasoning", {})
        reasoning_score = reasoning_data.score if hasattr(reasoning_data, "score") else reasoning_data.get("score", 0.0)
        
        instruction_data = probes.get("instruction", {})
        instruction_score = instruction_data.score if hasattr(instruction_data, "score") else instruction_data.get("score", 0.0)

        model.capabilities.coding = score_to_level(coding_score)
        model.capabilities.reasoning = score_to_level(reasoning_score)
        model.capabilities.instruction_following = score_to_level(instruction_score)

        # Infer other capabilities
        if model.capabilities.reasoning >= CapabilityLevel.INTERMEDIATE:
            model.capabilities.planning = CapabilityLevel.INTERMEDIATE
        if model.capabilities.instruction_following >= CapabilityLevel.INTERMEDIATE:
            model.capabilities.tool_use = CapabilityLevel.INTERMEDIATE

    async def _probe_model_background(self, model: ModelDescriptor, cache: ModelProfileCache) -> None:
        """Run probes in background, update model in registry when done."""
        try:
            from velune.kernel.registry import get_container
            container = get_container()
            if not container.has("runtime.provider_registry"):
                logger.debug("No provider registry registered yet, skipping background probe for %s.", model.model_id)
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
            logger.info("Successfully probed %s: coding=%.2f reasoning=%.2f", 
                        model.model_id, results["coding"].score, results["reasoning"].score)
        except Exception as e:
            logger.debug("Background probe failed for %s: %s", model.model_id, e)
