"""Reasoning Council agents package."""

from velune.cognition.council.base import BaseCouncilAgent
from velune.cognition.council.planner import PlannerAgent
from velune.cognition.council.coder import CoderAgent
from velune.cognition.council.reviewer import ReviewerAgent
from velune.cognition.council.challenger import ChallengerAgent
from velune.cognition.council.synthesizer import SynthesizerAgent
from velune.cognition.council.critic_agent import CriticAgent
from velune.cognition.council.critic_configs import (
    CriticConfig,
    SCALABILITY_CONFIG,
    SECURITY_CONFIG,
    PERFORMANCE_CONFIG,
    MAINTAINABILITY_CONFIG,
)
from velune.cognition.council.critics import (
    ScalabilityCritic,
    SecurityCritic,
    PerformanceCritic,
    MaintainabilityCritic,
)
from velune.cognition.council.debate import (
    DebateConfig,
    calculate_max_debate_turns,
)

__all__ = [
    "BaseCouncilAgent",
    "PlannerAgent",
    "CoderAgent",
    "ReviewerAgent",
    "ChallengerAgent",
    "SynthesizerAgent",
    "CriticAgent",
    "CriticConfig",
    "SCALABILITY_CONFIG",
    "SECURITY_CONFIG",
    "PERFORMANCE_CONFIG",
    "MAINTAINABILITY_CONFIG",
    "ScalabilityCritic",
    "SecurityCritic",
    "PerformanceCritic",
    "MaintainabilityCritic",
    "DebateConfig",
    "calculate_max_debate_turns",
]
