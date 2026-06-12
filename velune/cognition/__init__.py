"""Velune multi-agent Reasoning Council cognition package."""

from velune.cognition.arbitrator import ArbitrationResult, CouncilArbitrator
from velune.cognition.architecture import ArchitectureCognitionAgent, CognitiveDebtLedger
from velune.cognition.council_orchestrator import BoundedCouncilOrchestrator
from velune.cognition.firewall import CognitiveFirewall
from velune.cognition.orchestrator import CouncilOrchestrator
from velune.cognition.personality import RepositoryPersonalityAgent
from velune.cognition.verification import ReasoningVerifier

__all__ = [
    "CouncilArbitrator",
    "ArbitrationResult",
    # Full LangGraph-style multi-tier orchestrator
    "CouncilOrchestrator",
    # Simpler bounded 3-agent (Planner/Coder/Reviewer) orchestrator
    "BoundedCouncilOrchestrator",
    "CognitiveFirewall",
    "ReasoningVerifier",
    "ArchitectureCognitionAgent",
    "CognitiveDebtLedger",
    "RepositoryPersonalityAgent",
]
