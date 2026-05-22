"""Model capability store."""

from typing import Dict, Optional
from velune.core.types import ModelDescriptor, ModelCapability, CapabilityLevel
from velune.providers import ProviderRegistry


class ModelCapabilityStore:
    """Store for model capabilities across providers."""

    def __init__(self, provider_registry: ProviderRegistry):
        self.provider_registry = provider_registry
        self._models: Dict[str, ModelDescriptor] = {}
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize the store by loading models from all providers."""
        if self._initialized:
            return

        await self.provider_registry.initialize_all()
        
        for provider_name in self.provider_registry.list_providers():
            provider = self.provider_registry.get(provider_name)
            if provider:
                models = await provider.list_models()
                for model in models:
                    self._models[model.id] = model
        
        self._initialized = True

    def get_model(self, model_id: str) -> Optional[ModelDescriptor]:
        """Get a model descriptor by ID."""
        return self._models.get(model_id)

    def list_models(self) -> list[ModelDescriptor]:
        """List all available models."""
        return list(self._models.values())

    def find_models_by_capability(
        self,
        capability: ModelCapability,
        min_level: CapabilityLevel = CapabilityLevel.INTERMEDIATE,
    ) -> list[ModelDescriptor]:
        """Find models that have a capability at or above a level."""
        matching = []
        for model in self._models.values():
            level = model.capabilities.get(capability, CapabilityLevel.NONE)
            if self._level_at_least(level, min_level):
                matching.append(model)
        return matching

    def find_best_model_for_capability(
        self,
        capability: ModelCapability,
        provider: Optional[str] = None,
    ) -> Optional[ModelDescriptor]:
        """Find the best model for a capability."""
        candidates = self.find_models_by_capability(capability)
        
        if provider:
            candidates = [m for m in candidates if m.provider == provider]
        
        if not candidates:
            return None
        
        # Sort by capability level (expert > advanced > intermediate > basic)
        level_order = {
            CapabilityLevel.EXPERT: 4,
            CapabilityLevel.ADVANCED: 3,
            CapabilityLevel.INTERMEDIATE: 2,
            CapabilityLevel.BASIC: 1,
            CapabilityLevel.NONE: 0,
        }
        
        candidates.sort(
            key=lambda m: level_order.get(
                m.capabilities.get(capability, CapabilityLevel.NONE), 0
            ),
            reverse=True,
        )
        
        return candidates[0]

    def _level_at_least(
        self, level: CapabilityLevel, min_level: CapabilityLevel
    ) -> bool:
        """Check if a capability level is at least the minimum."""
        level_order = {
            CapabilityLevel.EXPERT: 4,
            CapabilityLevel.ADVANCED: 3,
            CapabilityLevel.INTERMEDIATE: 2,
            CapabilityLevel.BASIC: 1,
            CapabilityLevel.NONE: 0,
        }
        return level_order.get(level, 0) >= level_order.get(min_level, 0)
