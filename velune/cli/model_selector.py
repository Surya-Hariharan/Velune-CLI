from velune.cli.modes import ModeConfig
from velune.core.types.model import ModelDescriptor


class ModeAwareModelSelector:
    def __init__(self, model_registry, provider_registry) -> None:
        self.model_registry = model_registry
        self.provider_registry = provider_registry

    def select_for_mode(
        self,
        config: ModeConfig,
        current_model: ModelDescriptor | None,
    ) -> ModelDescriptor | None:
        all_models = [
            m for m in self.model_registry.list_all()
            if self.provider_registry.get(m.provider_id) is not None
        ]
        if not all_models:
            return current_model

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
                return sum([
                    getattr(caps, f, 0).value if hasattr(
                        getattr(caps, f, None), "value"
                    ) else 0
                    for f in ["coding", "reasoning", "planning",
                               "summarization", "instruction_following"]
                ])
            return max(all_models, key=capability_score)

        return current_model
