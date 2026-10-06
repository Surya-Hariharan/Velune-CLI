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
        *,
        fallback_provider_ids: tuple[str, ...] | list[str] = (),
        allow_cloud_fallback_from_local: bool = False,
        max_fallbacks: int = 2,
    ) -> None:
        self.provider_registry = provider_registry
        self.mapper = mapper
        self.live_lock = live_lock
        # Per-seat fallback: providers the user allows as alternates, in preference order.
        # Empty (the default) means a failed seat is simply unavailable.
        self.fallback_provider_ids = tuple(fallback_provider_ids)
        self.allow_cloud_fallback_from_local = allow_cloud_fallback_from_local
        self.max_fallbacks = max_fallbacks
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

    def _fallbacks_for(
        self, role: CouncilRole, primary: ModelDescriptor
    ) -> list[tuple[Any, ModelDescriptor]]:
        """(provider, model) alternates for a seat, from the mapper's deterministic chain."""
        chain_for = getattr(self.mapper, "fallback_chain", None)
        if chain_for is None or not self.fallback_provider_ids:
            return []
        usable = {
            pid
            for pid in self.fallback_provider_ids
            if self.provider_registry.check_provider_available(pid)
        }
        pairs: list[tuple[Any, ModelDescriptor]] = []
        for model in chain_for(
            role,
            primary,
            allowed_provider_ids=self.fallback_provider_ids,
            usable_provider_ids=usable,
            max_n=self.max_fallbacks,
            allow_local_to_cloud=self.allow_cloud_fallback_from_local,
        ):
            provider = self.provider_registry.get(model.provider_id)
            if provider is not None:
                pairs.append((provider, model))
        return pairs

    def _finish(self, agent: Any, role: CouncilRole, model: ModelDescriptor) -> Any:
        agent.live_lock = self.live_lock
        agent._fallback_providers = self._fallbacks_for(role, model)
        return agent

    def create_planner(self, run_id: str) -> PlannerAgent:
        roles = self.get_role_mapping(run_id)
        model = roles[CouncilRole.PLANNER]
        agent = PlannerAgent(
            model=model,
            provider=self.provider_registry.get_or_raise(model.provider_id),
        )
        return self._finish(agent, CouncilRole.PLANNER, model)

    def create_coder(self, run_id: str) -> CoderAgent:
        roles = self.get_role_mapping(run_id)
        model = roles[CouncilRole.CODER]
        agent = CoderAgent(
            model=model,
            provider=self.provider_registry.get_or_raise(model.provider_id),
        )
        return self._finish(agent, CouncilRole.CODER, model)

    def create_reviewer(self, run_id: str) -> ReviewerAgent:
        roles = self.get_role_mapping(run_id)
        model = roles[CouncilRole.REVIEWER]
        agent = ReviewerAgent(
            model=model,
            provider=self.provider_registry.get_or_raise(model.provider_id),
        )
        return self._finish(agent, CouncilRole.REVIEWER, model)

    def create_challenger(self, run_id: str) -> ChallengerAgent:
        roles = self.get_role_mapping(run_id)
        model = roles[CouncilRole.CHALLENGER]
        agent = ChallengerAgent(
            model=model,
            provider=self.provider_registry.get_or_raise(model.provider_id),
        )
        return self._finish(agent, CouncilRole.CHALLENGER, model)

    def create_synthesizer(self, run_id: str) -> SynthesizerAgent:
        roles = self.get_role_mapping(run_id)
        model = roles[CouncilRole.SYNTHESIZER]
        agent = SynthesizerAgent(
            model=model,
            provider=self.provider_registry.get_or_raise(model.provider_id),
        )
        return self._finish(agent, CouncilRole.SYNTHESIZER, model)

    def create_scalability_critic(self, run_id: str) -> ScalabilityCritic:
        roles = self.get_role_mapping(run_id)
        model = roles[CouncilRole.CHALLENGER]
        agent = ScalabilityCritic(
            model=model,
            provider=self.provider_registry.get_or_raise(model.provider_id),
        )
        return self._finish(agent, CouncilRole.CHALLENGER, model)

    def create_security_critic(self, run_id: str) -> SecurityCritic:
        roles = self.get_role_mapping(run_id)
        model = roles[CouncilRole.REVIEWER]
        agent = SecurityCritic(
            model=model,
            provider=self.provider_registry.get_or_raise(model.provider_id),
        )
        return self._finish(agent, CouncilRole.REVIEWER, model)

    def create_performance_critic(self, run_id: str) -> PerformanceCritic:
        roles = self.get_role_mapping(run_id)
        model = roles[CouncilRole.REVIEWER]
        agent = PerformanceCritic(
            model=model,
            provider=self.provider_registry.get_or_raise(model.provider_id),
        )
        return self._finish(agent, CouncilRole.REVIEWER, model)

    def create_maintainability_critic(self, run_id: str) -> MaintainabilityCritic:
        roles = self.get_role_mapping(run_id)
        model = roles[CouncilRole.REVIEWER]
        agent = MaintainabilityCritic(
            model=model,
            provider=self.provider_registry.get_or_raise(model.provider_id),
        )
        return self._finish(agent, CouncilRole.REVIEWER, model)
