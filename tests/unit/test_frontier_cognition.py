"""Comprehensive unit tests for the Velune Frontier Engineering Cognition Platform."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import pytest

from velune.cognition.architecture import ArchitectureCognitionAgent, CognitiveDebtLedger
from velune.cognition.council.critics import (
    ScalabilityCritic,
    SecurityCritic,
    PerformanceCritic,
    MaintainabilityCritic,
)
from velune.cognition.orchestrator import CouncilOrchestrator
from velune.cognition.arbitrator import CouncilArbitrator

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor
from velune.core.types.inference import InferenceRequest, InferenceResponse
from velune.core.types.provider import ProviderCapabilities, ProviderHealth
from velune.providers.base import ModelProvider
from velune.providers.registry import ProviderRegistry
from velune.models.registry import ModelCapabilityRegistry
from velune.models.specializations import ModelSpecializationMapper, CouncilRole


# =====================================================================
# 1. Mock LLM Model Provider for Orchestration and Debate Loops
# =====================================================================

class MockFrontierModelProvider(ModelProvider):
    def __init__(self) -> None:
        self.inferences: list[InferenceRequest] = []
        self.debate_triggered = False
        self.critic_call_count: dict[str, int] = {}

    @property
    def provider_id(self) -> str:
        return "mock-frontier-provider"

    async def list_models(self) -> list[ModelDescriptor]:
        return []

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        self.inferences.append(request)
        system_prompt = next((msg["content"] for msg in request.messages if msg["role"] == "system"), "")

        # Identify which critic/agent is calling based on system prompt
        role_key = None
        if "Senior Code Reviewer" in system_prompt:
            role_key = "reviewer"
        elif "Scalability Critic" in system_prompt:
            role_key = "scalability"
        elif "Security Critic" in system_prompt:
            role_key = "security"
        elif "Performance Critic" in system_prompt:
            role_key = "performance"
        elif "Maintainability Critic" in system_prompt:
            role_key = "maintainability"

        if role_key:
            self.critic_call_count[role_key] = self.critic_call_count.get(role_key, 0) + 1
            call_num = self.critic_call_count[role_key]
        else:
            call_num = 1

        if "Lead Planner" in system_prompt:
            content = """
            {
              "task_id": "frontier-task-001",
              "steps": [
                {
                  "id": "design_interfaces",
                  "description": "Design unified boundary interfaces",
                  "agent_role": "coder",
                  "dependencies": [],
                  "metadata": {
                    "command": "echo Unified > boundaries.py",
                    "expected_files": ["boundaries.py"]
                  }
                }
              ]
            }
            """
        elif "Lead Coder" in system_prompt:
            content = "class DatabaseConnectionPool:\n    def __init__(self):\n        self.conns = []"
        elif "Senior Code Reviewer" in system_prompt:
            if call_num > 1:
                content = '{"passed": true, "critical_issues": [], "suggestions": [], "confidence_rating": 0.99}'
            else:
                self.debate_triggered = True
                content = '{"passed": false, "critical_issues": ["Reviewer detected thread-safety vulnerability"], "confidence_rating": 0.4}'
        elif "Adversarial Challenger" in system_prompt:
            content = '{"assumptions_challenged": ["Python version"], "failure_vectors": [], "severity_rating": 0.1}'
        elif "Scalability Critic" in system_prompt:
            if call_num > 1:
                content = '{"passed": true, "issues": [], "score": 0.95, "rationale": "Optimized connection pool size"}'
            else:
                self.debate_triggered = True
                content = '{"passed": false, "issues": ["High contention in concurrent acquires"], "score": 0.35, "rationale": "Thread contention"}'
        elif "Security Critic" in system_prompt:
            content = '{"passed": true, "issues": [], "score": 0.98, "rationale": "Sanitized path parameters"}'
        elif "Performance Critic" in system_prompt:
            content = '{"passed": true, "issues": [], "score": 0.94, "rationale": "Negligible memory overhead"}'
        elif "Maintainability Critic" in system_prompt:
            content = '{"passed": true, "issues": [], "score": 0.96, "rationale": "Clean Single Responsibility pattern"}'
        elif "Lead Synthesizer" in system_prompt:
            content = "Synthesized final high-concurrency database connection pooling solution successfully."
        else:
            content = "Default mock content"

        return InferenceResponse(
            content=content,
            model_id=request.model_id,
            finish_reason="stop",
            tokens_used=200,
            latency_ms=10.0,
        )

    async def stream(self, request: InferenceRequest):
        raise NotImplementedError()

    async def embed(self, texts: list[str], model_id: str) -> list[list[float]]:
        return [[0.1] * 64 for _ in texts]

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(healthy=True)

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


# =====================================================================
# 2. Cohesion and Boundary Checking Unit Tests
# =====================================================================

def test_lcom_cohesion_calculation():
    agent = ArchitectureCognitionAgent(workspace_root=None)

    # Class 1: Extremely Cohesive (LCOM = 0)
    cohesive_code = """
class CohesiveCalculator:
    def __init__(self):
        self.val = 0
        self.ops = 0

    def add(self, x):
        self.val += x
        self.ops += 1

    def subtract(self, x):
        self.val -= x
        self.ops += 1
"""
    scores_cohesive = agent.calculate_lcom(cohesive_code)
    assert "CohesiveCalculator" in scores_cohesive
    assert scores_cohesive["CohesiveCalculator"] == 0

    # Class 2: Incohesive (LCOM > 0)
    incohesive_code = """
class IncohesiveGodClass:
    def __init__(self):
        self.x = 10
        self.y = 20
        self.z = 30

    def calculate_x(self):
        return self.x * 2

    def calculate_y(self):
        return self.y * 3

    def calculate_z(self):
        return self.z * 4

    def reset_all(self):
        # Disjoint methods don't overlap on attributes
        pass
"""
    scores_incohesive = agent.calculate_lcom(incohesive_code)
    assert "IncohesiveGodClass" in scores_incohesive
    # methods are: __init__, calculate_x, calculate_y, calculate_z, reset_all.
    # calculate_x accesses self.x; calculate_y accesses self.y; calculate_z accesses self.z.
    # So they are completely disjoint. Thus LCOM score > 0.
    assert scores_incohesive["IncohesiveGodClass"] > 0


def test_boundary_guard_layering_violations():
    agent = ArchitectureCognitionAgent(workspace_root=None)

    # Disallowed: core importing execution, cognition, or cli layers
    violating_code = """
import sys
from velune.cognition.orchestrator import CouncilOrchestrator
from velune.cli.commands import run_cli

def run():
    print("Violating core layer rules")
"""
    violations = agent.verify_boundaries(violating_code, "velune/core/main.py")
    assert len(violations) > 0
    assert any("imports 'velune.cognition.orchestrator'" in v for v in violations)

    # Allowed: core importing kernel or third-party
    allowed_code = """
import os
from velune.kernel.lifecycle import LifecycleManager

def run():
    print("Clean separation")
"""
    violations_allowed = agent.verify_boundaries(allowed_code, "velune/core/main.py")
    assert len(violations_allowed) == 0


def test_cognitive_debt_ledger_persistence(tmp_path):
    ledger_file = tmp_path / "test_debt_ledger.json"
    ledger = CognitiveDebtLedger(ledger_path=ledger_file)

    # Verify initial ledger is empty
    assert len(ledger.get_items()) == 0

    # Add items
    ledger.add_debt_item("velune/core/main.py", "layering", "Layer violation details", 1.5)
    ledger.add_debt_item("velune/cognition/architecture.py", "cohesion", "High LCOM index", 0.8)

    # Re-instantiate to verify persistence load
    ledger_new = CognitiveDebtLedger(ledger_path=ledger_file)
    items = ledger_new.get_items()
    assert len(items) == 2
    assert any(item["category"] == "layering" and item["severity"] == 1.5 for item in items)
    assert ledger_new.data["total_severity"] == 2.3

    # Clear one file's debt
    ledger_new.clear_file_debt("velune/core/main.py")
    assert len(ledger_new.get_items()) == 1
    assert ledger_new.data["total_severity"] == 0.8


# =====================================================================
# 3. Critics Council and Complexity Routing Unit Tests
# =====================================================================

def test_complexity_routing_thresholds():
    # Setup mock registry/mapper
    provider_registry = ProviderRegistry(config=None)
    model_registry = ModelCapabilityRegistry(scanner=None)
    mapper = ModelSpecializationMapper(registry=model_registry)
    orchestrator = CouncilOrchestrator(provider_registry, mapper)

    # Simple Tasks (Should bypass parallel critics)
    assert orchestrator._is_structural_change("Fix typo in UI error message text", "") is False
    assert orchestrator._is_structural_change("Add a single line docstring comment", "") is False

    # Structural Tasks (Should trigger parallel critics)
    assert orchestrator._is_structural_change("Redesign database connection pooling with high concurrency locks", "") is True
    assert orchestrator._is_structural_change("Implement thread-safe event loop with LCOM boundaries", "") is True
    assert orchestrator._is_structural_change("Refactor the model registry classes to support customizable dynamic routing", "") is True


@pytest.mark.asyncio
async def test_parallel_critics_execution():
    # Setup Mock Provider
    mock_provider = MockFrontierModelProvider()
    model = ModelDescriptor(
        id="test-brain",
        provider="mock-frontier-provider",
        name="Test Brain",
        context_window=4096,
        is_local=False,
        capabilities=ModelCapabilityProfile(reasoning=CapabilityLevel.ADVANCED),
        speed_tier="fast",
    )

    # Instantiate critics
    critics = [
        ScalabilityCritic(model, mock_provider),
        SecurityCritic(model, mock_provider),
        PerformanceCritic(model, mock_provider),
        MaintainabilityCritic(model, mock_provider),
    ]

    proposal = "def pool_conns(): pass"
    context = "# Connection Pooling"

    # Execute all critics concurrently
    tasks = [c.critique("Database Pooling", proposal, context) for c in critics]
    reports = await asyncio.gather(*tasks)

    # Verify distinct audits
    assert len(reports) == 4
    scalability_report = reports[0]
    security_report = reports[1]
    performance_report = reports[2]
    maintainability_report = reports[3]

    assert scalability_report["passed"] is False  # Scalability fails initially in mock
    assert "High contention" in scalability_report["issues"][0]
    assert security_report["passed"] is True
    assert performance_report["passed"] is True
    assert maintainability_report["passed"] is True


@pytest.mark.asyncio
async def test_contradiction_driven_debate_loop_convergence(tmp_path):
    # Setup model, registry, mapper
    provider_registry = ProviderRegistry(config=None)
    mock_provider = MockFrontierModelProvider()
    provider_registry.register("mock-frontier-provider", mock_provider)

    model_registry = ModelCapabilityRegistry(scanner=None)
    adv_model = ModelDescriptor(
        id="advanced-brain",
        provider="mock-frontier-provider",
        name="Advanced Brain",
        context_window=32768,
        is_local=False,
        capabilities=ModelCapabilityProfile(
            coding=CapabilityLevel.EXPERT,
            reasoning=CapabilityLevel.EXPERT,
            planning=CapabilityLevel.ADVANCED,
            summarization=CapabilityLevel.ADVANCED,
        ),
        speed_tier="fast",
    )
    model_registry.register(adv_model)

    mapper = ModelSpecializationMapper(registry=model_registry)
    orchestrator = CouncilOrchestrator(
        provider_registry=provider_registry,
        mapper=mapper,
        historical_accuracy=0.85,
    )

    # Setup the ledger file in temporary directory
    orchestrator.architecture_agent.ledger = CognitiveDebtLedger(ledger_path=tmp_path / "debt.json")

    # A complex/structural prompt triggering ACA and the debate council
    prompt = "Redesign database connection pooling with high concurrency"
    repo_context = "class Conn:\n    def connect(self): pass"

    # Execute orchestration
    result = await orchestrator.execute_task(prompt, repo_context)

    # Assert that ACA and Critics triggered a debate loop and resolved it!
    assert mock_provider.debate_triggered is True
    assert result["task_plan"] is not None
    assert result["coder_proposal"] is not None
    assert result["reviewer_report"]["passed"] is True  # Converged to pass on turn 2
    assert result["scalability_report"]["passed"] is True  # Converged to pass on turn 2

    # Arbitration checks
    arbitration = result["arbitration"]
    assert not arbitration["requires_human_review"]
    assert "All specialized critics approved the proposed implementation." in arbitration["winning_claims"]
    assert arbitration["overall_confidence"] > 0.85
