"""Multi-factor scorer for model routing and selection with family-specific and quantization-aware adjustments."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional
from velune.core.types.model import ModelDescriptor, CapabilityLevel
from velune.models.profiler import ModelProfile

logger = logging.getLogger("velune.models.scorer")


class ModelScorer:
    """Calculates multidimensional matching scores for model selection."""

    def __init__(
        self,
        w_capability: float = 0.4,
        w_context: float = 0.2,
        w_speed: float = 0.2,
        w_reliability: float = 0.1,
        w_cost: float = 0.1,
    ) -> None:
        """Initialize routing weights."""
        self.w_capability = w_capability
        self.w_context = w_context
        self.w_speed = w_speed
        self.w_reliability = w_reliability
        self.w_cost = w_cost

    def _detect_model_family(self, model_id: str) -> str:
        """Detect model family from ID for family-specific scoring adjustments."""
        lower = model_id.lower()
        families = {
            "qwen": ["qwen"],
            "deepseek": ["deepseek"],
            "llama": ["llama", "meta-llama"],
            "mistral": ["mistral", "mixtral"],
            "phi": ["phi"],
            "gemma": ["gemma"],
            "codellama": ["codellama"],
            "starcoder": ["starcoder"],
        }
        for family, patterns in families.items():
            if any(p in lower for p in patterns):
                return family
        return "unknown"

    def _get_family_capability_adjustments(self, family: str, task_category: str) -> float:
        """
        Return capability score adjustments for known model families.
        Positive = boost, Negative = penalty.
        Based on community benchmarks and known model strengths.
        """
        adjustments = {
            ("qwen", "coding"): +0.1,
            ("qwen", "reasoning"): +0.05,
            ("deepseek", "coding"): +0.15,
            ("deepseek", "reasoning"): +0.1,
            ("codellama", "coding"): +0.2,
            ("codellama", "reasoning"): -0.1,  # Not a reasoning model
            ("phi", "coding"): +0.05,
            ("phi", "reasoning"): +0.15,  # Phi is surprisingly capable at reasoning for its size
            ("mistral", "summarization"): +0.1,
            ("gemma", "instruction_following"): +0.05,
            ("starcoder", "coding"): +0.2,
            ("starcoder", "reasoning"): -0.15,
        }
        return adjustments.get((family, task_category), 0.0)

    def _get_quantization_penalty(self, model: ModelDescriptor) -> float:
        """
        Quantization reduces quality. Apply penalty for heavily quantized models
        on reasoning and complex tasks.
        """
        quant = (model.quantization or "").upper()
        penalties = {
            "Q2": -0.25,
            "Q3": -0.15,
            "Q4_0": -0.08,
            "Q4_K_M": -0.05,
            "Q5": -0.02,
            "Q5_K_M": -0.02,
            "Q8_0": 0.0,
            "FP16": +0.05,  # Slight quality boost for full precision
        }
        return penalties.get(quant, 0.0)

    def score(
        self,
        model: ModelDescriptor,
        task_category: str,
        required_tokens: int = 0,
        latency_requirement: str = "medium",
        profile: Optional[ModelProfile] = None,
        local_preferred: bool = False,
    ) -> float:
        """
        Calculate aggregated suitability score (0.0 - 1.0) for a model based on task constraints.
        
        agg_score = w_cap * cap_match + w_ctx * ctx_fit + w_speed * speed + w_rel * reliability - w_cost * cost
        """
        # 1. Base Capability Score (0.0 to 1.0)
        cap_score = self._calculate_capability_score(model, task_category)

        # Apply model family adjustments
        family = self._detect_model_family(model.model_id)
        family_adj = self._get_family_capability_adjustments(family, task_category)
        cap_score = max(0.0, min(1.0, cap_score + family_adj))

        # Apply quantization penalty for reasoning-heavy tasks
        if task_category in ("reasoning", "planning"):
            quant_penalty = self._get_quantization_penalty(model)
            cap_score = max(0.0, min(1.0, cap_score + quant_penalty))

        # 2. Context Fit Score (0.0 to 1.0)
        ctx_score = self._calculate_context_score(model.context_length, required_tokens)

        # 3. Speed / Performance Score (0.0 to 1.0)
        speed_score = self._calculate_speed_score(model, latency_requirement, profile)

        # 4. Reliability / Validity Score (0.0 to 1.0)
        reliability_score = self._calculate_reliability_score(model, profile, local_preferred)

        # 5. Cost Penalty (0.0 to 1.0)
        cost_penalty = self._calculate_cost_penalty(model)

        # Aggregate weighted components
        total_score = (
            self.w_capability * cap_score +
            self.w_context * ctx_score +
            self.w_speed * speed_score +
            self.w_reliability * reliability_score -
            self.w_cost * cost_penalty
        )

        return max(0.0, min(1.0, total_score))

    def _calculate_capability_score(self, model: ModelDescriptor, task_category: str) -> float:
        """Evaluate how well model capability level matches task category."""
        capabilities = getattr(model, "capabilities", None)
        if not capabilities:
            return 0.25  # Basic fallback

        level = CapabilityLevel.NONE
        # Extract capability level from Pydantic model or dictionary representation
        if isinstance(capabilities, dict):
            level_val = capabilities.get(task_category, CapabilityLevel.NONE)
            if isinstance(level_val, int):
                level = CapabilityLevel(level_val)
        else:
            if hasattr(capabilities, task_category):
                level = getattr(capabilities, task_category)

        # Score mapping
        level_score_map = {
            CapabilityLevel.NONE: 0.0,
            CapabilityLevel.BASIC: 0.2,
            CapabilityLevel.INTERMEDIATE: 0.5,
            CapabilityLevel.ADVANCED: 0.8,
            CapabilityLevel.EXPERT: 1.0,
        }

        return level_score_map.get(level, 0.2)

    def _calculate_context_score(self, context_length: int, required_tokens: int) -> float:
        """Evaluate how well context window size fits required token limits."""
        if required_tokens <= 0:
            return 1.0

        if context_length >= required_tokens:
            # Having extra headroom is good, but value decays as ratio grows
            ratio = context_length / required_tokens
            return min(1.0, 0.8 + 0.2 / ratio)
        else:
            # Severe penalty for context overflow
            return max(0.0, (context_length / required_tokens) * 0.5)

    def _calculate_speed_score(self, model: ModelDescriptor, latency_requirement: str, profile: Optional[ModelProfile]) -> float:
        """Calculate speed score using empirical metrics (TPS/TTFT) if available, falling back to static tiers."""
        # Dynamic scoring if profile metrics exist
        if profile and profile.tps > 0:
            # Estimate speed based on empirical tokens per second. (assume 80 TPS is maximum optimal score)
            empirical_tps_score = min(1.0, profile.tps / 80.0)
            
            # Penalize long TTFT (assume > 1.5 seconds starts decaying score)
            ttft_penalty = max(0.0, min(0.5, (profile.ttft_ms - 1500.0) / 3000.0)) if profile.ttft_ms > 0 else 0.0
            return max(0.1, empirical_tps_score - ttft_penalty)

        # Fallback to static speed tiers
        speed_map = {"fast": 1.0, "medium": 0.6, "slow": 0.3}
        model_speed = speed_map.get(model.speed_tier, 0.6)

        req_map = {"fast": 1.0, "medium": 0.6, "slow": 0.3}
        req_speed = req_map.get(latency_requirement, 0.6)

        if model_speed >= req_speed:
            return 1.0
        return model_speed / req_speed

    def _calculate_reliability_score(self, model: ModelDescriptor, profile: Optional[ModelProfile], local_preferred: bool) -> float:
        """Determine reliability and preference score based on locality and validation history."""
        score = 0.9  # Baseline reliability

        if model.is_local:
            # Boost if local models are requested
            score += 0.1 if local_preferred else 0.05
        else:
            # Slight penalty if we strictly prefer local running
            score -= 0.2 if local_preferred else 0.0

        # Empirical JSON formatting validity penalty
        if profile and profile.json_validity < 1.0:
            score -= (1.0 - profile.json_validity) * 0.5

        return max(0.0, min(1.0, score))

    def _calculate_cost_penalty(self, model: ModelDescriptor) -> float:
        """Calculate score penalty based on token cost."""
        cost = model.cost_per_1k_tokens
        if cost is None or cost <= 0.0:
            return 0.0  # Zero cost for local offline models

        # Standardize cost penalty (assuming max expected cost is $0.15 per 1k tokens)
        return min(1.0, cost / 0.15)
