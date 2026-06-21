from __future__ import annotations

from typing import TYPE_CHECKING, Any

from velune.cognition.council.challenger import ChallengerAgent
from velune.cognition.council.coder import CoderAgent
from velune.cognition.council.critics import (
    MaintainabilityCritic,
    PerformanceCritic,
    ScalabilityCritic,
    SecurityCritic,
)
from velune.cognition.council.planner import PlannerAgent
from velune.cognition.council.reviewer import ReviewerAgent
from velune.cognition.council.synthesizer import SynthesizerAgent
from velune.models.specializations import CouncilRole

if TYPE_CHECKING:
    from velune.core.types.model import ModelDescriptor
    from velune.models.specializations import ModelSpecializationMapper
    from velune.providers.registry import ProviderRegistry


class CouncilAgentFactory:
    """Centralized factory to construct specialized Reasoning Council agents, caching role mappings per run."""

    def __init__(
        self,
        provider_registry: ProviderRegistry,
        mapper: ModelSpecializationMapper,
        live_lock: Any | None = None,
    ) -> None:
        self.provider_registry = provider_registry
        self.mapper = mapper
        self.live_lock = live_lock
        # Cache of resolved role mappings by run_id
        self._mappings_cache: dict[str, dict[CouncilRole, ModelDescriptor]] = {}

    def get_role_mapping(self, run_id: str) -> dict[CouncilRole, ModelDescriptor]:
        """Retrieve or compute the model mapping for a given run ID."""
        if run_id not in self._mappings_cache:
            self._mappings_cache[run_id] = self.mapper.map_roles()
        return self._mappings_cache[run_id]

    def clear_cache(self, run_id: str | None = None) -> None:
        """Clear the role mapping cache, either globally or for a specific run."""
        if run_id:
            self._mappings_cache.pop(run_id, None)
        else:
            self._mappings_cache.clear()

    def create_planner(self, run_id: str) -> PlannerAgent:
        roles = self.get_role_mapping(run_id)
        model = roles[CouncilRole.PLANNER]
        agent = PlannerAgent(
            model=model,
            provider=self.provider_registry.get_or_raise(model.provider_id),
        )
        agent.live_lock = self.live_lock
        return agent

    def create_coder(self, run_id: str) -> CoderAgent:
        roles = self.get_role_mapping(run_id)
        model = roles[CouncilRole.CODER]
        agent = CoderAgent(
            model=model,
            provider=self.provider_registry.get_or_raise(model.provider_id),
        )
        agent.live_lock = self.live_lock
        return agent

    def create_reviewer(self, run_id: str) -> ReviewerAgent:
        roles = self.get_role_mapping(run_id)
        model = roles[CouncilRole.REVIEWER]
        agent = ReviewerAgent(
            model=model,
            provider=self.provider_registry.get_or_raise(model.provider_id),
        )
        agent.live_lock = self.live_lock
        return agent

    def create_challenger(self, run_id: str) -> ChallengerAgent:
        roles = self.get_role_mapping(run_id)
        model = roles[CouncilRole.CHALLENGER]
        agent = ChallengerAgent(
            model=model,
            provider=self.provider_registry.get_or_raise(model.provider_id),
        )
        agent.live_lock = self.live_lock
        return agent

    def create_synthesizer(self, run_id: str) -> SynthesizerAgent:
        roles = self.get_role_mapping(run_id)
        model = roles[CouncilRole.SYNTHESIZER]
        agent = SynthesizerAgent(
            model=model,
            provider=self.provider_registry.get_or_raise(model.provider_id),
        )
        agent.live_lock = self.live_lock
        return agent

    def create_scalability_critic(self, run_id: str) -> ScalabilityCritic:
        roles = self.get_role_mapping(run_id)
        model = roles[CouncilRole.CHALLENGER]
        agent = ScalabilityCritic(
            model=model,
            provider=self.provider_registry.get_or_raise(model.provider_id),
        )
        agent.live_lock = self.live_lock
        return agent

    def create_security_critic(self, run_id: str) -> SecurityCritic:
        roles = self.get_role_mapping(run_id)
        model = roles[CouncilRole.REVIEWER]
        agent = SecurityCritic(
            model=model,
            provider=self.provider_registry.get_or_raise(model.provider_id),
        )
        agent.live_lock = self.live_lock
        return agent

    def create_performance_critic(self, run_id: str) -> PerformanceCritic:
        roles = self.get_role_mapping(run_id)
        model = roles[CouncilRole.REVIEWER]
        agent = PerformanceCritic(
            model=model,
            provider=self.provider_registry.get_or_raise(model.provider_id),
        )
        agent.live_lock = self.live_lock
        return agent

    def create_maintainability_critic(self, run_id: str) -> MaintainabilityCritic:
        roles = self.get_role_mapping(run_id)
        model = roles[CouncilRole.REVIEWER]
        agent = MaintainabilityCritic(
            model=model,
            provider=self.provider_registry.get_or_raise(model.provider_id),
        )
        agent.live_lock = self.live_lock
        return agent
