"""Multi-factor scoring for model routing."""

from typing import Dict, Optional
from velune.core.types.model import ModelDescriptor, CapabilityLevel
from velune.core.types.task import Task


class RoutingScore:
    """Multi-factor scoring function for model routing."""

    def __init__(
        self,
        w_capability: float = 0.4,
        w_context: float = 0.2,
        w_speed: float = 0.15,
        w_availability: float = 0.15,
        w_cost: float = 0.1,
    ):
        """Initialize scoring weights."""
        self.w_capability = w_capability
        self.w_context = w_context
        self.w_speed = w_speed
        self.w_availability = w_availability
        self.w_cost = w_cost

    def score(
        self,
        model: ModelDescriptor,
        task: Optional[Task] = None,
        required_tokens: int = 0,
        latency_requirement: str = "medium",
    ) -> float:
        """
        Calculate routing score for a model.
        
        score(model, task) = w₁·capability_match(model, task)
                           + w₂·context_fit(model.context_length, required_tokens)
                           + w₃·speed_score(model, task.latency_requirement)
                           + w₄·availability_score(model)
                           - w₅·cost_penalty(model, task)
        """
        capability_score = self._capability_match(model, task)
        context_score = self._context_fit(model.context_length, required_tokens)
        speed_score = self._speed_score(model, latency_requirement)
        availability_score = self._availability_score(model)
        cost_penalty = self._cost_penalty(model)
        
        total = (
            self.w_capability * capability_score +
            self.w_context * context_score +
            self.w_speed * speed_score +
            self.w_availability * availability_score -
            self.w_cost * cost_penalty
        )
        
        return max(0.0, min(1.0, total))

    def _capability_match(self, model: ModelDescriptor, task: Optional[Task]) -> float:
        """Score capability match between model and task."""
        if not task:
            return 0.5
        
        # Map task requirements to model capabilities
        task_capability_map = {
            "coding": model.capabilities.coding,
            "reasoning": model.capabilities.reasoning,
            "planning": model.capabilities.planning,
            "summarization": model.capabilities.summarization,
        }
        
        # Get the relevant capability based on task type
        task_type = task.task_type.value if task else "general"
        capability = task_capability_map.get(task_type, CapabilityLevel.BASIC)
        
        # Convert capability level to score
        level_score = {
            CapabilityLevel.NONE: 0.0,
            CapabilityLevel.BASIC: 0.25,
            CapabilityLevel.CAPABLE: 0.5,
            CapabilityLevel.STRONG: 0.75,
            CapabilityLevel.EXCEPTIONAL: 1.0,
        }
        
        return level_score.get(capability, 0.5)

    def _context_fit(self, context_length: int, required_tokens: int) -> float:
        """Score how well model context fits required tokens."""
        if required_tokens == 0:
            return 1.0
        
        if context_length >= required_tokens:
            # Extra context is good but diminishing returns
            ratio = context_length / required_tokens
            return min(1.0, 0.8 + 0.2 / ratio)
        else:
            # Insufficient context
            return context_length / required_tokens

    def _speed_score(self, model: ModelDescriptor, latency_requirement: str) -> float:
        """Score model speed against latency requirement."""
        speed_map = {"fast": 1.0, "medium": 0.7, "slow": 0.4}
        model_speed = speed_map.get(model.speed_tier, 0.7)
        
        req_map = {"fast": 1.0, "medium": 0.7, "slow": 0.4}
        req_speed = req_map.get(latency_requirement, 0.7)
        
        # Model should meet or exceed requirement
        if model_speed >= req_speed:
            return 1.0
        else:
            return model_speed / req_speed

    def _availability_score(self, model: ModelDescriptor) -> float:
        """Score model availability (placeholder)."""
        # In production, this would check actual availability
        # For now, assume local models are always available
        return 1.0 if "local" in model.tags else 0.9

    def _cost_penalty(self, model: ModelDescriptor) -> float:
        """Calculate cost penalty."""
        if model.cost_per_1k_tokens is None:
            return 0.0  # Local models have no cost
        
        # Normalize cost (assuming max reasonable cost is $0.10 per 1k tokens)
        return min(1.0, model.cost_per_1k_tokens / 0.1)
