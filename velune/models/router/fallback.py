"""Fallback chain management."""

from typing import list, Optional
from velune.core.types import ModelDescriptor
from velune.models.registry.store import ModelCapabilityStore


class FallbackChain:
    """Manages fallback chains for model routing."""

    def __init__(self, capability_store: ModelCapabilityStore):
        self.capability_store = capability_store

    def get_fallback_chain(
        self, primary_model: ModelDescriptor
    ) -> list[ModelDescriptor]:
        """Get fallback chain for a model."""
        fallbacks = []
        
        # Get all models from the same provider
        same_provider = [
            m for m in self.capability_store.list_models()
            if m.provider == primary_model.provider and m.id != primary_model.id
        ]
        fallbacks.extend(same_provider)
        
        # Get models from other providers
        other_providers = [
            m for m in self.capability_store.list_models()
            if m.provider != primary_model.provider
        ]
        fallbacks.extend(other_providers)
        
        return fallbacks

    def get_next_fallback(
        self, current_model: ModelDescriptor, failed_models: list[str]
    ) -> Optional[ModelDescriptor]:
        """Get the next fallback model."""
        chain = self.get_fallback_chain(current_model)
        
        for model in chain:
            if model.id not in failed_models:
                return model
        
        return None
