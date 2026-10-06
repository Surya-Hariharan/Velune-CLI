"""Model specialization mapper for the Reasoning Council with role-specific context optimizations."""

from __future__ import annotations

import logging

from velune._compat import StrEnum
from velune.core.types.model import ModelDescriptor
from velune.models.profiler import ModelProfiler
from velune.models.registry import ModelCapabilityRegistry
from velune.models.scorer import ModelScorer

logger = logging.getLogger("velune.models.specializations")


class CouncilRole(StrEnum):
    """Roles in the Velune Reasoning Council."""

    PLANNER = "planner"
    CODER = "coder"
    REVIEWER = "reviewer"
    CHALLENGER = "challenger"
    SYNTHESIZER = "synthesizer"


ROLE_CONTEXT_REQUIREMENTS = {
    CouncilRole.PLANNER: 16384,  # Needs full repo context
    CouncilRole.CODER: 32768,  # Needs code + context + plan
    CouncilRole.REVIEWER: 32768,  # Needs to see full code
    CouncilRole.CHALLENGER: 16384,  # Needs code summary
    CouncilRole.SYNTHESIZER: 65536,  # Needs all outputs
}


# (scorer task category, latency preference) per role — the same routing map_roles uses.
ROLE_ROUTING: dict[CouncilRole, tuple[str, str]] = {
    CouncilRole.PLANNER: ("planning", "medium"),
    CouncilRole.CODER: ("coding", "medium"),
    CouncilRole.REVIEWER: ("reasoning", "slow"),
    CouncilRole.CHALLENGER: ("reasoning", "medium"),
    CouncilRole.SYNTHESIZER: ("summarization", "fast"),
}


class ModelSpecializationMapper:
    """Intelligent mapper that assigns discovered models to council roles based on scoring and role-specific context bounds."""

    def __init__(
        self,
        registry: ModelCapabilityRegistry,
        scorer: ModelScorer | None = None,
        profiler: ModelProfiler | None = None,
    ) -> None:
        self.registry = registry
        self.scorer = scorer or ModelScorer()
        self.profiler = profiler or ModelProfiler()
        self.overrides: dict[CouncilRole, str] = {}

    def map_roles(
        self,
        task_category: str = "coding",
        required_tokens: int | None = None,
        local_preferred: bool = False,
    ) -> dict[CouncilRole, ModelDescriptor]:
        """
        Assigns the best available model for each CouncilRole based on their functional profiles and optimal context token sizes.

        - Planner: High planning and instruction-following scores (optimizes for 16k context window).
        - Coder: High coding and tool-use scores (optimizes for 32k context window).
        - Reviewer: High reasoning and instruction-following scores (optimizes for 32k context window).
        - Challenger: High reasoning and adversarial analysis capabilities (optimizes for 16k context window).
        - Synthesizer: High summarization and context window capability (optimizes for 64k context window).
        """
        models = self.registry.list_all()
        if not models:
            logger.warning(
                "No models found in the capability registry. Council mappings will be empty."
            )
            return {}

        assignments: dict[CouncilRole, ModelDescriptor] = {}

        def get_tokens(role: CouncilRole) -> int:
            if required_tokens is not None:
                if role == CouncilRole.REVIEWER:
                    return required_tokens + 2048
                elif role == CouncilRole.SYNTHESIZER:
                    return required_tokens + 4096
                else:
                    return required_tokens
            return ROLE_CONTEXT_REQUIREMENTS[role]

        # 1. Map Planner
        planner_model = self._select_best_model(
            models=models,
            role_category="planning",
            required_tokens=get_tokens(CouncilRole.PLANNER),
            latency_requirement="medium",
            local_preferred=local_preferred,
        )
        if planner_model:
            assignments[CouncilRole.PLANNER] = planner_model

        # 2. Map Coder
        coder_model = self._select_best_model(
            models=models,
            role_category="coding",
            required_tokens=get_tokens(CouncilRole.CODER),
            latency_requirement="medium",
            local_preferred=local_preferred,
        )
        if coder_model:
            assignments[CouncilRole.CODER] = coder_model

        # 3. Map Reviewer (prefers slower, highly capable reasoning models)
        reviewer_model = self._select_best_model(
            models=models,
            role_category="reasoning",
            required_tokens=get_tokens(CouncilRole.REVIEWER),
            latency_requirement="slow",
            local_preferred=local_preferred,
        )
        if reviewer_model:
            assignments[CouncilRole.REVIEWER] = reviewer_model

        # 4. Map Challenger (needs strong reasoning)
        challenger_model = self._select_best_model(
            models=models,
            role_category="reasoning",
            required_tokens=get_tokens(CouncilRole.CHALLENGER),
            latency_requirement="medium",
            local_preferred=local_preferred,
        )
        if challenger_model:
            assignments[CouncilRole.CHALLENGER] = challenger_model

        # 5. Map Synthesizer (prefers faster summarization models with large context)
        synthesizer_model = self._select_best_model(
            models=models,
            role_category="summarization",
            required_tokens=get_tokens(CouncilRole.SYNTHESIZER),
            latency_requirement="fast",
            local_preferred=local_preferred,
        )
        if synthesizer_model:
            assignments[CouncilRole.SYNTHESIZER] = synthesizer_model

        # Ensure we have fallbacks for all roles if any fail to map
        if models:
            default_model = models[0]
            for role in CouncilRole:
                if role not in assignments:
                    logger.info(
                        "Falling back role %s to default model %s",
                        role.value,
                        default_model.model_id,
                    )
                    assignments[role] = default_model

        # Apply explicitly assigned overrides
        for role, overridden_model_id in self.overrides.items():
            descriptor = self.registry.get(overridden_model_id)
            if descriptor:
                assignments[role] = descriptor

        return assignments

    def fallback_chain(
        self,
        role: CouncilRole,
        primary: ModelDescriptor,
        *,
        allowed_provider_ids: list[str] | tuple[str, ...],
        usable_provider_ids: set[str] | None = None,
        max_n: int = 2,
        allow_local_to_cloud: bool = False,
    ) -> list[ModelDescriptor]:
        """Alternate models for *role* if *primary* fails, in a fixed order.

        A pure function of its arguments and the registry contents: the same inputs give
        the same chain whatever order models were discovered in. Providers are taken in
        the order of *allowed_provider_ids* (the user's preference), never the primary's
        own provider, and only if usable. Per provider the best-scoring model for the
        role wins, ties broken by model id. A local primary never falls back to a cloud
        model unless *allow_local_to_cloud* is set.
        """
        if max_n <= 0 or not allowed_provider_ids:
            return []
        category, latency = ROLE_ROUTING[role]
        models = self.registry.list_all()
        chain: list[ModelDescriptor] = []
        seen = {primary.provider_id}
        skipped_cloud: list[str] = []
        for provider_id in allowed_provider_ids:
            if provider_id in seen:
                continue
            seen.add(provider_id)
            if usable_provider_ids is not None and provider_id not in usable_provider_ids:
                continue
            candidates = sorted(
                (m for m in models if m.provider_id == provider_id), key=lambda m: m.model_id
            )
            if primary.is_local and not allow_local_to_cloud:
                local_only = [m for m in candidates if m.is_local]
                if candidates and not local_only:
                    skipped_cloud.append(provider_id)
                candidates = local_only
            if not candidates:
                continue
            best = self._select_best_model(
                models=candidates,
                role_category=category,
                required_tokens=ROLE_CONTEXT_REQUIREMENTS[role],
                latency_requirement=latency,
                local_preferred=primary.is_local,
            )
            if best is not None:
                chain.append(best)
            if len(chain) >= max_n:
                break
        if skipped_cloud and not chain:
            logger.info(
                "No fallback for %s: %s are cloud providers and the primary %s is local "
                "(providers.allow_cloud_fallback_from_local is off).",
                role.value,
                ", ".join(skipped_cloud),
                primary.model_id,
            )
        return chain

    def _select_best_model(
        self,
        models: list[ModelDescriptor],
        role_category: str,
        required_tokens: int,
        latency_requirement: str,
        local_preferred: bool,
    ) -> ModelDescriptor | None:
        """Helper to score all models and select the highest scoring candidate."""
        try:
            from velune.kernel.registry import get_container

            gpu_info = get_container().get("runtime.gpu_info")
            available_vram_gb = gpu_info.get("vram_free_gb")
        except Exception:
            available_vram_gb = None

        best_model: ModelDescriptor | None = None
        best_score = -1.0

        for model in models:
            # VRAM check for local models
            if model.is_local and available_vram_gb is not None:
                required_vram = model.vram_required_gb
                if required_vram and required_vram > available_vram_gb:
                    logger.info(
                        "Skipping %s: requires %.1fGB VRAM, only %.1fGB available",
                        model.model_id,
                        required_vram,
                        available_vram_gb,
                    )
                    continue  # Skip models that won't fit in VRAM

            profile = self.profiler.get_profile(model.provider_id, model.model_id)
            score = self.scorer.score(
                model=model,
                task_category=role_category,
                required_tokens=required_tokens,
                latency_requirement=latency_requirement,
                profile=profile,
                local_preferred=local_preferred,
            )

            if score > best_score:
                best_score = score
                best_model = model

        return best_model


def detect_model_collapse(
    role_mapping: dict[CouncilRole, ModelDescriptor],
) -> bool:
    """Return ``True`` when every role resolved to the same physical model.

    On a single local GPU only one model fits in VRAM, so the mapper assigns it
    to all roles — "one model wearing different hats". Callers surface this as
    ``degraded_diversity`` to the UI and compensate by drawing more Coder
    samples (see :func:`velune.cognition.council.sampling.coder_sample_count`).
    """
    model_ids = {desc.model_id for desc in role_mapping.values()}
    return len(model_ids) <= 1 and len(role_mapping) > 1
