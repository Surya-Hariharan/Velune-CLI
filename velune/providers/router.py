"""Provider routing subsystem matching tasks to capabilities."""

from __future__ import annotations

import logging

from velune.core.types.model import CapabilityLevel, ModelDescriptor
from velune.providers.registry import ProviderRegistry

logger = logging.getLogger("velune.providers.router")


class ProviderRouter:
    """Intelligent router selecting optimal model profiles based on task constraints."""

    def __init__(self, provider_registry: ProviderRegistry) -> None:
        self._provider_registry = provider_registry
        self._fallback_chain: list[str] = ["openai", "anthropic", "ollama"]

    def set_fallback_chain(self, fallback_chain: list[str]) -> None:
        """Set the provider routing fallback sequence."""
        self._fallback_chain = fallback_chain

    def route_task(
        self,
        task_category: str,
        models_list: list[ModelDescriptor],
        min_level: CapabilityLevel = CapabilityLevel.BASIC,
        local_preferred: bool = False,
    ) -> ModelDescriptor | None:
        """Selects the best available model descriptor that satisfies the profile constraints."""
        candidates = models_list

        # Filter candidates by capability tier
        qualified: list[ModelDescriptor] = []
        for model in candidates:
            # Check profile matches if present
            profile = getattr(model, "capabilities", None)
            if profile:
                score = 0
                if hasattr(profile, task_category):
                    score = int(getattr(profile, task_category))
                elif task_category in getattr(profile, "__dict__", {}):
                    score = int(profile.__dict__[task_category])

                if score >= int(min_level):
                    qualified.append(model)
            else:
                # Default assume qualified if profile is unpopulated
                qualified.append(model)

        if not qualified:
            qualified = candidates

        if not qualified:
            logger.warning("No models found in registry to route task: %s", task_category)
            return None

        # Priority 1: Local vs Remote
        if local_preferred:
            local_options = [m for m in qualified if getattr(m, "is_local", False)]
            if local_options:
                return self._select_best_by_speed_or_context(local_options)

        # Priority 2: Match by provider rank in fallback chain
        for provider in self._fallback_chain:
            provider_options = [m for m in qualified if m.provider_id == provider]
            if provider_options:
                return self._select_best_by_speed_or_context(provider_options)

        # Fallback: Just return the first matching qualified candidate
        return qualified[0]

    def _select_best_by_speed_or_context(self, options: list[ModelDescriptor]) -> ModelDescriptor:
        """Heuristic selector favoring larger context bounds or faster tiers."""
        # Sort by context window descending, and then speed tier (fast first)
        sorted_opts = sorted(
            options,
            key=lambda x: (x.context_length, 1 if getattr(x, "speed_tier", "medium") == "fast" else 0),
            reverse=True,
        )
        return sorted_opts[0]
