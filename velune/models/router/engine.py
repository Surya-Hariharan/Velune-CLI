"""Routing decision engine with multi-factor scoring."""

from typing import Optional
from velune.core.types.model import ModelDescriptor
from velune.core.types.task import Task
from velune.models.registry.store import ModelCapabilityStore
from velune.models.registry.assignments import AssignmentStore
from velune.models.router.strategies import RoutingStrategy
from velune.models.router.scoring import RoutingScore


class RoutingEngine:
    """Engine for making routing decisions with multi-factor scoring."""

    def __init__(
        self,
        capability_store: ModelCapabilityStore,
        assignment_store: AssignmentStore,
        strategy: RoutingStrategy = RoutingStrategy.LOCAL_FIRST,
    ):
        self.capability_store = capability_store
        self.assignment_store = assignment_store
        self._strategy = strategy
        self.scorer = RoutingScore()

    def set_strategy(self, strategy: RoutingStrategy) -> None:
        """Set the routing strategy."""
        self._strategy = strategy

    async def route_for_role(
        self,
        role: str,
        task: Optional[Task] = None,
        required_tokens: int = 0,
        latency_requirement: str = "medium",
    ) -> Optional[ModelDescriptor]:
        """Route to the best model for a role."""
        # First check if there's an explicit assignment
        assigned_model_id = self.assignment_store.get_assignment(role)
        if assigned_model_id:
            model = self.capability_store.get_model(assigned_model_id)
            if model:
                return model
        
        # Fall back to strategy-based routing
        return await self._select_by_strategy(
            task,
            required_tokens,
            latency_requirement,
        )

    async def _select_by_strategy(
        self,
        task: Optional[Task],
        required_tokens: int,
        latency_requirement: str,
    ) -> Optional[ModelDescriptor]:
        """Select model based on routing strategy."""
        all_models = [
            self.capability_store.get_model(model_id)
            for model_id in self.capability_store.list_models()
        ]
        all_models = [m for m in all_models if m is not None]

        if not all_models:
            return None

        if self._strategy == RoutingStrategy.QUALITY_FIRST:
            return self._quality_first_selection(all_models, task)
        elif self._strategy == RoutingStrategy.SPEED_FIRST:
            return self._speed_first_selection(all_models, task, latency_requirement)
        elif self._strategy == RoutingStrategy.COST_AWARE:
            return self._cost_aware_selection(all_models, task)
        elif self._strategy == RoutingStrategy.LOCAL_FIRST:
            return self._local_first_selection(all_models, task)
        else:
            return all_models[0]

    def _quality_first_selection(
        self,
        models: list[ModelDescriptor],
        task: Optional[Task],
    ) -> ModelDescriptor:
        """Maximize capability match, ignore speed and cost."""
        scored = [
            (model, self.scorer.score(model, task))
            for model in models
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[0][0]

    def _speed_first_selection(
        self,
        models: list[ModelDescriptor],
        task: Optional[Task],
        latency_requirement: str,
    ) -> ModelDescriptor:
        """Prefer fastest models meeting minimum capability threshold."""
        from velune.core.types.model import CapabilityLevel
        
        # Filter by minimum capability (BASIC)
        capable_models = [
            m for m in models
            if m.capabilities.coding.value >= CapabilityLevel.BASIC.value
            or m.capabilities.reasoning.value >= CapabilityLevel.BASIC.value
        ]

        if not capable_models:
            capable_models = models

        # Sort by speed tier
        speed_order = {"fast": 0, "medium": 1, "slow": 2}
        capable_models.sort(key=lambda m: speed_order.get(m.speed_tier, 1))
        return capable_models[0]

    def _cost_aware_selection(
        self,
        models: list[ModelDescriptor],
        task: Optional[Task],
    ) -> ModelDescriptor:
        """Minimize cost while meeting quality requirements (cloud models only)."""
        cloud_models = [m for m in models if m.cost_per_1k_tokens is not None]
        
        if not cloud_models:
            return models[0]

        # Sort by cost
        cloud_models.sort(key=lambda m: m.cost_per_1k_tokens or float("inf"))
        return cloud_models[0]

    def _local_first_selection(
        self,
        models: list[ModelDescriptor],
        task: Optional[Task],
    ) -> ModelDescriptor:
        """Prefer local models, fall back to cloud only when capability gap exists."""
        local_models = [m for m in models if "local" in m.tags]
        
        if local_models:
            # Score local models
            scored = [
                (model, self.scorer.score(model, task))
                for model in local_models
            ]
            scored.sort(key=lambda x: x[1], reverse=True)
            
            # If best local model has decent score, use it
            if scored[0][1] >= 0.5:
                return scored[0][0]
        
        # Fall back to cloud models
        cloud_models = [m for m in models if "cloud" in m.tags]
        if cloud_models:
            scored = [
                (model, self.scorer.score(model, task))
                for model in cloud_models
            ]
            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[0][0]
        
        return models[0]
