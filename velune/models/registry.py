"""Model capability registry."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional
from velune.core.types.model import ModelDescriptor, ModelCapabilityProfile, CapabilityLevel
from velune.providers.discovery.scanner import ModelDiscoveryScanner

logger = logging.getLogger("velune.models.registry")


class ModelCapabilityRegistry:
    """Unified database cataloging discovered models and capabilities."""

    def __init__(self, scanner: Optional[ModelDiscoveryScanner] = None) -> None:
        self.scanner = scanner or ModelDiscoveryScanner()
        self._models: Dict[str, ModelDescriptor] = {}

    async def refresh(self) -> None:
        """Scan all providers and refresh the local catalog cache."""
        try:
            discovered = await self.scanner.scan_all()
            self._models.clear()
            for model in discovered:
                # Store under a fully qualified key: provider_id/model_id
                key = f"{model.provider_id}/{model.model_id}"
                self._models[key] = model
                # Also store under simple model_id if not already present
                if model.model_id not in self._models:
                    self._models[model.model_id] = model
            logger.info("Successfully indexed %d active models.", len(discovered))
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
