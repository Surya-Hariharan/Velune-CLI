from __future__ import annotations

from velune.cli.modes import ModeConfig
from velune.core.types.model import ModelDescriptor
from velune.hardware.profiles import RuntimeProfile


def fits_hardware(model: ModelDescriptor, profile: RuntimeProfile | None) -> bool:
    """True when a local model is safe to run under the active runtime profile.

    Cloud models always fit. Local models without size metadata are assumed to
    fit (discovery may not have populated parameter counts yet).
    """
    if profile is None or not model.is_local:
        return True
    if model.parameter_count_b is not None:
        return model.parameter_count_b <= profile.max_local_model_b
    return True


class ModeAwareModelSelector:
    def __init__(self, model_registry, provider_registry, runtime_profile=None) -> None:
        self.model_registry = model_registry
        self.provider_registry = provider_registry
        self.runtime_profile = runtime_profile

    def select_for_mode(
        self,
        config: ModeConfig,
        current_model: ModelDescriptor | None,
    ) -> ModelDescriptor | None:
        all_models = [
            m
            for m in self.model_registry.list_all()
            if m.is_local or self.provider_registry.check_provider_available(m.provider_id)
        ]
        if not all_models:
            return current_model

        # Drop local models the machine cannot run comfortably; if that empties
        # the pool entirely, fall back to the unfiltered list rather than fail.
        safe = [m for m in all_models if fits_hardware(m, self.runtime_profile)]
        if safe:
            all_models = safe

        if config.use_fastest_model:
            # Smallest context window = fastest / lightest
            # Prefer local 3B/7B over cloud in optimus
            local = [m for m in all_models if m.is_local]
            pool = local if local else all_models
            return min(pool, key=lambda m: m.context_length)

        if config.use_largest_model:
            # Largest context + highest capability score
            def capability_score(m: ModelDescriptor) -> int:
                caps = m.capabilities
                if not caps:
                    return 0
                return sum(
                    [
                        getattr(caps, f, 0).value if hasattr(getattr(caps, f, None), "value") else 0
                        for f in [
                            "coding",
                            "reasoning",
                            "planning",
                            "summarization",
                            "instruction_following",
                        ]
                    ]
                )

            return max(all_models, key=capability_score)

        return current_model
