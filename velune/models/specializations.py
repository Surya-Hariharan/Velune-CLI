"""Model specialization mapper for the Reasoning Council."""

from __future__ import annotations

import logging
from enum import Enum
from typing import Dict, List, Optional
from velune.core.types.model import ModelDescriptor
from velune.models.registry import ModelCapabilityRegistry
from velune.models.scorer import ModelScorer
from velune.models.profiler import ModelProfiler

logger = logging.getLogger("velune.models.specializations")


class CouncilRole(str, Enum):
    """Roles in the Velune Reasoning Council."""

    PLANNER = "planner"
    CODER = "coder"
    REVIEWER = "reviewer"
    CHALLENGER = "challenger"
    SYNTHESIZER = "synthesizer"


class ModelSpecializationMapper:
    """Intelligent mapper that assigns discovered models to council roles based on scoring."""

    def __init__(
        self,
        registry: ModelCapabilityRegistry,
        scorer: Optional[ModelScorer] = None,
        profiler: Optional[ModelProfiler] = None,
    ) -> None:
        self.registry = registry
        self.scorer = scorer or ModelScorer()
        self.profiler = profiler or ModelProfiler()
        self.overrides: Dict[CouncilRole, str] = {}

    def map_roles(
        self,
        task_category: str = "coding",
        required_tokens: int = 4096,
        local_preferred: bool = False,
    ) -> Dict[CouncilRole, ModelDescriptor]:
        """
        Assigns the best available model for each CouncilRole based on their functional profiles.
        
        - Planner: High planning and instruction-following scores.
        - Coder: High coding and tool-use scores.
        - Reviewer: High reasoning and instruction-following scores (typically large models).
        - Challenger: High reasoning and adversarial analysis capabilities.
        - Synthesizer: High summarization and context window capability (prefers fast models).
        """
        models = self.registry.list_all()
        if not models:
            logger.warning("No models found in the capability registry. Council mappings will be empty.")
            return {}

        assignments: Dict[CouncilRole, ModelDescriptor] = {}

        # 1. Map Planner
        planner_model = self._select_best_model(
            models=models,
            role_category="planning",
            required_tokens=required_tokens,
            latency_requirement="medium",
            local_preferred=local_preferred,
        )
        if planner_model:
            assignments[CouncilRole.PLANNER] = planner_model

        # 2. Map Coder
        coder_model = self._select_best_model(
            models=models,
            role_category="coding",
            required_tokens=required_tokens,
            latency_requirement="medium",
            local_preferred=local_preferred,
        )
        if coder_model:
            assignments[CouncilRole.CODER] = coder_model

        # 3. Map Reviewer (prefers slower, highly capable reasoning models)
        reviewer_model = self._select_best_model(
            models=models,
            role_category="reasoning",
            required_tokens=required_tokens + 2048,  # Add cushion for code context
            latency_requirement="slow",
            local_preferred=local_preferred,
        )
        if reviewer_model:
            assignments[CouncilRole.REVIEWER] = reviewer_model

        # 4. Map Challenger (needs strong reasoning)
        challenger_model = self._select_best_model(
            models=models,
            role_category="reasoning",
            required_tokens=required_tokens,
            latency_requirement="medium",
            local_preferred=local_preferred,
        )
        if challenger_model:
            assignments[CouncilRole.CHALLENGER] = challenger_model

        # 5. Map Synthesizer (prefers faster summarization models with large context)
        synthesizer_model = self._select_best_model(
            models=models,
            role_category="summarization",
            required_tokens=required_tokens + 4096,  # Usually digests large context
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
                    logger.info("Falling back role %s to default model %s", role.value, default_model.model_id)
                    assignments[role] = default_model

        # Apply explicitly assigned overrides
        for role, overridden_model_id in self.overrides.items():
            descriptor = self.registry.get(overridden_model_id)
            if descriptor:
                assignments[role] = descriptor

        return assignments

    def _select_best_model(
        self,
        models: List[ModelDescriptor],
        role_category: str,
        required_tokens: int,
        latency_requirement: str,
        local_preferred: bool,
    ) -> Optional[ModelDescriptor]:
        """Helper to score all models and select the highest scoring candidate."""
        best_model: Optional[ModelDescriptor] = None
        best_score = -1.0

        for model in models:
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
