"""Provider routing subsystem matching tasks to capabilities using real probe scores."""

from __future__ import annotations

import logging

from velune.core.types.model import CapabilityLevel, ModelDescriptor
from velune.core.types.provider import ProviderHealth
from velune.providers.health import get_checker
from velune.providers.registry import ProviderRegistry

logger = logging.getLogger("velune.providers.router")

_LOCAL_PROVIDER_IDS = frozenset({"ollama", "lmstudio", "llamacpp"})

# Warn once per process about offline mode
_offline_warned: bool = False


class ProviderRouter:
    """Intelligent router selecting optimal model profiles based on empirical probe scores."""

    def __init__(self, provider_registry: ProviderRegistry) -> None:
        self._provider_registry = provider_registry
        self._fallback_chain: list[str] = ["openai", "anthropic", "ollama"]
        self._connectivity = get_checker()
        self._health_monitor = None

    def set_fallback_chain(self, fallback_chain: list[str]) -> None:
        """Set the provider routing fallback sequence."""
        self._fallback_chain = fallback_chain

    def set_health_monitor(self, monitor) -> None:
        """Set the health monitor for real-time provider status checking."""
        self._health_monitor = monitor

    def _filter_by_health(self, models: list[ModelDescriptor]) -> list[ModelDescriptor]:
        """Filter models by provider health status.

        Only considers models from available providers. Prefers HEALTHY providers over DEGRADED.
        """
        if not self._health_monitor:
            return models

        # Separate into healthy, degraded, and unavailable
        healthy: list[ModelDescriptor] = []
        degraded: list[ModelDescriptor] = []

        for model in models:
            manifest = self._health_monitor.get_manifest(model.provider_id)
            if not manifest:
                # No manifest yet, assume healthy
                healthy.append(model)
                continue

            if manifest.health == ProviderHealth.HEALTHY:
                healthy.append(model)
            elif manifest.health == ProviderHealth.DEGRADED:
                degraded.append(model)
            # Skip unavailable providers

        # Return healthy first, then degraded
        return healthy if healthy else degraded

    def route_task(
        self,
        task_category: str,
        models_list: list[ModelDescriptor],
        min_level: CapabilityLevel = CapabilityLevel.BASIC,
        local_preferred: bool = False,
        latency_sensitive: bool = False,
    ) -> ModelDescriptor | None:
        """Selects the best available model using empirical probe scores.

        Strategy:
        1. Filter by provider health (unavailable providers excluded)
        2. Filter to models meeting min_level capability requirement
        3. Sort by relevant probe score (descending)
        4. Apply local preference: if local model >= 0.85 × best_score, prefer local
        5. Apply latency preference: for latency_sensitive tasks, weight speed score

        When internet is unavailable, cloud providers are bypassed and local models
        are preferred. A one-time warning is emitted to stderr the first time this
        offline fallback activates.
        """
        global _offline_warned

        # Filter by health status first
        candidates = self._filter_by_health(models_list)
        if not candidates:
            logger.warning(
                "No providers available after health filtering; falling back to all models"
            )
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
            local_options = [
                m
                for m in qualified
                if getattr(m, "is_local", False) or m.provider_id in _LOCAL_PROVIDER_IDS
            ]
            if local_options:
                return self._select_best_by_score(local_options, task_category, latency_sensitive)

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

        # Priority 2: Route by capability score + local preference
        selected = self._route_by_capability_score(qualified, task_category, latency_sensitive)
        if selected:
            return selected

        # Fallback: return first qualified candidate
        return qualified[0]

    def _route_by_capability_score(
        self,
        qualified: list[ModelDescriptor],
        task_category: str,
        latency_sensitive: bool,
    ) -> ModelDescriptor | None:
        """Route using empirical probe scores with local preference heuristic."""
        local_models = [
            m
            for m in qualified
            if getattr(m, "is_local", False) or m.provider_id in _LOCAL_PROVIDER_IDS
        ]
        [m for m in qualified if m not in local_models]

        # Get best score from all models
        best_score = self._get_task_score(qualified[0], task_category)
        best_model = None
        for model in qualified:
            score = self._get_task_score(model, task_category)
            if score > best_score:
                best_score = score
                best_model = model

        if best_model is None:
            best_model = qualified[0]

        # Check if local model is within 85% of best score (local preference threshold)
        if local_models:
            best_local = max(local_models, key=lambda m: self._get_task_score(m, task_category))
            local_score = self._get_task_score(best_local, task_category)

            if local_score >= (0.85 * best_score):
                logger.debug(
                    "Local model %s (score %.2f) is competitive with best (%.2f), preferring local",
                    best_local.model_id,
                    local_score,
                    best_score,
                )
                return best_local

        return best_model

    def _select_best_by_score(
        self,
        options: list[ModelDescriptor],
        task_category: str,
        latency_sensitive: bool,
    ) -> ModelDescriptor:
        """Sort models by task capability score, with latency consideration."""

        def score_fn(model: ModelDescriptor) -> tuple:
            task_score = self._get_task_score(model, task_category)

            # For latency-sensitive tasks, prefer low latency
            latency_score = 0.0
            if latency_sensitive and self._health_monitor:
                manifest = self._health_monitor.get_manifest(model.provider_id)
                if manifest:
                    # Normalize latency: prefer <100ms (score 1.0), 100-500ms (0.5-1.0), >500ms (0.0-0.5)
                    latency_ms = manifest.estimated_latency_ms
                    if latency_ms < 100:
                        latency_score = 1.0
                    elif latency_ms < 500:
                        latency_score = 0.5 + (500 - latency_ms) / (400 * 2)
                    else:
                        latency_score = max(0.0, 0.5 - (latency_ms - 500) / 1000)
                else:
                    latency_score = 0.5  # Unknown latency

            # Return tuple for multi-key sort
            return (task_score, latency_score) if latency_sensitive else (task_score, 0.0)

        sorted_opts = sorted(options, key=score_fn, reverse=True)
        return sorted_opts[0]

    def get_ordered_candidates(
        self,
        task_category: str,
        models_list: list[ModelDescriptor],
        exclude_provider_ids: list[str] | None = None,
        min_level: CapabilityLevel = CapabilityLevel.BASIC,
        local_preferred: bool = False,
        latency_sensitive: bool = False,
    ) -> list[ModelDescriptor]:
        """Return all viable candidates in capability-score order, excluding specified providers.

        This supports retry/fallback patterns: callers try the first candidate,
        and on failure call again with ``exclude_provider_ids`` containing the
        failed provider's ID to get the next-best option.
        """
        exclude = set(exclude_provider_ids or [])
        filtered = [m for m in models_list if m.provider_id not in exclude]
        if not filtered:
            return []

        candidates = self._filter_by_health(filtered)
        if not candidates:
            candidates = filtered

        qualified: list[ModelDescriptor] = []
        for model in candidates:
            profile = getattr(model, "capabilities", None)
            if profile:
                score = 0
                if hasattr(profile, task_category):
                    score = int(getattr(profile, task_category))
                if score >= int(min_level):
                    qualified.append(model)
            else:
                qualified.append(model)

        if not qualified:
            return []

        online = self._connectivity.is_online
        effective_local_preferred = local_preferred or (not online)

        def _sort_key(m: ModelDescriptor) -> float:
            base = self._get_task_score(m, task_category)
            is_local = getattr(m, "is_local", False) or m.provider_id in _LOCAL_PROVIDER_IDS
            if effective_local_preferred and is_local:
                base += 0.01
            return base

        return sorted(qualified, key=_sort_key, reverse=True)

    def _get_task_score(self, model: ModelDescriptor, task_category: str) -> float:
        """Extract numeric probe score (0.0-1.0) from model capability profile."""
        profile = getattr(model, "capabilities", None)
        if not profile:
            return 0.5

        # Handle CapabilityLevel enum (0, 25, 50, 75, 100)
        if hasattr(profile, task_category):
            level = getattr(profile, task_category)
            if isinstance(level, int):
                return level / 100.0
            return 0.5

        # Fallback
        return 0.5
