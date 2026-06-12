"""Provider routing subsystem matching tasks to capabilities."""

from __future__ import annotations

import logging

from velune.core.types.model import CapabilityLevel, ModelDescriptor
from velune.providers.health import get_checker
from velune.providers.registry import ProviderRegistry

logger = logging.getLogger("velune.providers.router")

_LOCAL_PROVIDER_IDS = frozenset({"ollama", "lmstudio", "llamacpp"})

# Warn once per process about offline mode
_offline_warned: bool = False


class ProviderRouter:
    """Intelligent router selecting optimal model profiles based on task constraints."""

    def __init__(self, provider_registry: ProviderRegistry) -> None:
        self._provider_registry = provider_registry
        self._fallback_chain: list[str] = ["openai", "anthropic", "ollama"]
        self._connectivity = get_checker()

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
        """Selects the best available model descriptor that satisfies the profile constraints.

        When internet is unavailable, cloud providers are bypassed and local models
        are preferred.  A one-time warning is emitted to stderr the first time this
        offline fallback activates.
        """
        global _offline_warned

        candidates = models_list

        # Filter candidates by capability tier
        qualified: list[ModelDescriptor] = []
        for model in candidates:
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
            logger.warning("No models found in registry to route task: %s", task_category)
            return None

        # Offline detection: force local-only routing when internet is unavailable
        online = self._connectivity.is_online
        effective_local_preferred = local_preferred or (not online)

        if not online:
            if not _offline_warned:
                _offline_warned = True
                try:
                    from rich.console import Console
                    Console().print(
                        "[yellow]Offline mode: routing all tasks to local models[/yellow]"
                    )
                except Exception:
                    import sys
                    print("Offline mode: routing all tasks to local models", file=sys.stderr)

        # Priority 1: Local models (always first when offline or explicitly preferred)
        if effective_local_preferred:
            local_options = [m for m in qualified if getattr(m, "is_local", False)
                             or m.provider_id in _LOCAL_PROVIDER_IDS]
            if local_options:
                return self._select_best_by_speed_or_context(local_options)

            if not online:
                # No local models available and we're offline — fail with a clear message
                from velune.core.errors import NoModelsAvailableError
                raise NoModelsAvailableError(
                    "No internet connection and no local models configured for this task type",
                    cause_override=(
                        "The router detected no internet connectivity and found no local "
                        "models (Ollama, LM Studio, llama.cpp) capable of handling "
                        f"'{task_category}' tasks."
                    ),
                )

        # Priority 2: Match by provider rank in fallback chain (cloud-only when online)
        for provider in self._fallback_chain:
            provider_options = [m for m in qualified if m.provider_id == provider]
            # Skip cloud providers when offline
            if not online and provider not in _LOCAL_PROVIDER_IDS:
                continue
            if provider_options:
                return self._select_best_by_speed_or_context(provider_options)

        # Fallback: return first qualified candidate
        return qualified[0]

    def _select_best_by_speed_or_context(self, options: list[ModelDescriptor]) -> ModelDescriptor:
        """Heuristic selector favoring larger context bounds or faster tiers."""
        sorted_opts = sorted(
            options,
            key=lambda x: (x.context_length, 1 if getattr(x, "speed_tier", "medium") == "fast" else 0),
            reverse=True,
        )
        return sorted_opts[0]
