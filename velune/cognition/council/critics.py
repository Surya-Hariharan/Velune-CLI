"""Specialized Reasoning Council critics auditing system attributes."""

from __future__ import annotations

from velune.cognition.council.critic_agent import CriticAgent
from velune.cognition.council.critic_configs import (
    MAINTAINABILITY_CONFIG,
    PERFORMANCE_CONFIG,
    SCALABILITY_CONFIG,
    SECURITY_CONFIG,
)
from velune.core.types.model import ModelDescriptor
from velune.providers.base import ModelProvider


class ScalabilityCritic(CriticAgent):
    """Audits implementation plans for scaling bottlenecks and algorithmic efficiency."""

    def __init__(self, model: ModelDescriptor, provider: ModelProvider) -> None:
        super().__init__(SCALABILITY_CONFIG, model, provider)


class SecurityCritic(CriticAgent):
    """Audits changes for safety, input boundaries, and execution containment."""

    def __init__(self, model: ModelDescriptor, provider: ModelProvider) -> None:
        super().__init__(SECURITY_CONFIG, model, provider)


class PerformanceCritic(CriticAgent):
    """Audits implementations for runtime speed, RAM bounds, and payload latency."""

    def __init__(self, model: ModelDescriptor, provider: ModelProvider) -> None:
        super().__init__(PERFORMANCE_CONFIG, model, provider)


class MaintainabilityCritic(CriticAgent):
    """Audits classes cohesion, coupling, testability, and architectural maintainability."""

    def __init__(self, model: ModelDescriptor, provider: ModelProvider) -> None:
        super().__init__(MAINTAINABILITY_CONFIG, model, provider)
