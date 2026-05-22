"""Comprehensive unit tests for Velune persistent lineage memory and cognitive continuity."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
import pytest

from velune.memory.tiers.lineage import LineageMemoryTier
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
# 1. Thread-Safe Persistence Unit Tests
# =====================================================================

def test_lineage_tier_db_initialization(tmp_path):
    db_file = tmp_path / "continuity.db"
    tier = LineageMemoryTier(db_file)
    
    # Assert database file is created
    assert db_file.exists()
    
    # Clean shutdown
    tier.shutdown()


def test_dls_decision_persistence(tmp_path):
    db_file = tmp_path / "continuity.db"
    tier = LineageMemoryTier(db_file)
    
    # Log a decision with alternatives
    decision_id = "DEC-001"
    subsystem = "database_locks"
    rationale = "Switched to SQLite write queue to handle Windows file-locking under concurrent testing"
    impact = 0.8
    consequences = "Zero locked database errors observed after migration."
    alternatives = [
        {
            "option_name": "SQLite Write Queue",
            "tradeoffs": {"latency": "low", "concurrency": "high"},
            "rejected_reason": "",
        },
        {
            "option_name": "Raw Mutex Locking",
            "tradeoffs": {"latency": "medium", "concurrency": "low"},
            "rejected_reason": "High contention in concurrent acquires",
        }
    ]
    
    tier.log_decision(
        decision_id=decision_id,
        target_subsystem=subsystem,
        rationale=rationale,
        architectural_impact=impact,
        consequences=consequences,
        alternatives=alternatives,
    )
    
    # Force queue processing finish
    tier.write_queue.join()
    
    # Query decisions
    decisions = tier.get_subsystem_decisions("database")
    assert len(decisions) == 1
    dec = decisions[0]
    
    assert dec["id"] == decision_id
    assert dec["target_subsystem"] == subsystem
    assert dec["rationale"] == rationale
    assert dec["architectural_impact"] == impact
    assert dec["consequences"] == consequences
    
    assert len(dec["alternatives"]) == 2
    assert dec["alternatives"][0]["option_name"] == "SQLite Write Queue"
    assert dec["alternatives"][0]["tradeoffs"]["concurrency"] == "high"
    assert dec["alternatives"][1]["option_name"] == "Raw Mutex Locking"
    assert "High contention" in dec["alternatives"][1]["rejected_reason"]
    
    tier.shutdown()


def test_fel_failed_experiment_persistence(tmp_path):
    db_file = tmp_path / "continuity.db"
    tier = LineageMemoryTier(db_file)
    
    # Log a failed experiment
    subsystem = "concurrency_locks"
    patch = "def acquire_lock():\n    conn.execute('BEGIN IMMEDIATE')"
    error_type = "sqlite3.OperationalError"
    error_message = "database is locked"
    
    tier.log_failed_experiment(
        target_subsystem=subsystem,
        patch=patch,
        error_type=error_type,
        error_message=error_message,
    )
    
    # Force queue processing finish
    tier.write_queue.join()
    
    # Query failed experiments
    failures = tier.get_failed_experiments("concurrency")
    assert len(failures) == 1
    fail = failures[0]
    
    assert fail["target_subsystem"] == subsystem
    assert fail["patch"] == patch
    assert fail["error_type"] == error_type
    assert fail["error_message"] == error_message
    
    tier.shutdown()


def test_fuzzy_continuity_query_deduplication(tmp_path):
    db_file = tmp_path / "continuity.db"
    tier = LineageMemoryTier(db_file)
    
    # Log multiple overlapping decisions and failures
    tier.log_decision("DEC-100", "database", "First DB decision", 0.3)
    tier.log_decision("DEC-101", "database_concurrency", "Second DB decision", 0.5)
    tier.log_failed_experiment("concurrency", "patch A", "compile_error", "failed")
    
    # Force queue
    tier.write_queue.join()
    
    # Query warning blocks
    decisions, failures = tier.query_continuity_warnings(
        prompt="Setup a high-concurrency database engine",
        repo_context="class DatabaseConnectionPool: pass"
    )
    
    # Assert deduplicated & correctly mapped
    assert len(decisions) == 2
    assert len(failures) == 1
    assert any(d["id"] == "DEC-100" for d in decisions)
    assert any(d["id"] == "DEC-101" for d in decisions)
    assert failures[0]["patch"] == "patch A"
    
    tier.shutdown()


# =====================================================================
# 2. Mock model provider and warning injection integration tests
# =====================================================================

class MockLineageModelProvider(ModelProvider):
    def __init__(self) -> None:
        self.inferences: list[InferenceRequest] = []
        self.last_planner_prompt = ""

    @property
    def provider_id(self) -> str:
        return "mock-lineage-provider"

    async def list_models(self) -> list[ModelDescriptor]:
        return []

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        self.inferences.append(request)
        system_prompt = next((msg["content"] for msg in request.messages if msg["role"] == "system"), "")
        user_prompt = next((msg["content"] for msg in request.messages if msg["role"] == "user"), "")

        if "Lead Planner" in system_prompt:
            self.last_planner_prompt = user_prompt
            content = """
            {
              "task_id": "lineage-task-001",
              "steps": [
                {
                  "id": "write_lineage",
                  "description": "Initialize lineage tracking classes",
                  "agent_role": "coder",
                  "dependencies": [],
                  "metadata": {
                    "command": "echo Tracking > lineage.py",
                    "expected_files": ["lineage.py"]
                  }
                }
              ]
            }
            """
        elif "Lead Coder" in system_prompt:
            content = "class LineageTracker:\n    def __init__(self):\n        pass"
        elif "Senior Code Reviewer" in system_prompt:
            content = '{"passed": true, "critical_issues": [], "suggestions": [], "confidence_rating": 0.98}'
        elif "Adversarial Challenger" in system_prompt:
            content = '{"assumptions_challenged": [], "failure_vectors": [], "severity_rating": 0.1}'
        elif "Scalability Critic" in system_prompt:
            content = '{"passed": true, "issues": [], "score": 0.95, "rationale": "High efficiency"}'
        elif "Security Critic" in system_prompt:
            content = '{"passed": true, "issues": [], "score": 0.98, "rationale": "Secure imports"}'
        elif "Performance Critic" in system_prompt:
            content = '{"passed": true, "issues": [], "score": 0.94, "rationale": "Negligible footprint"}'
        elif "Maintainability Critic" in system_prompt:
            content = '{"passed": true, "issues": [], "score": 0.96, "rationale": "Cohesive methods"}'
        elif "Lead Synthesizer" in system_prompt:
            content = "Successfully compiled and verified lineage continuity integration."
        else:
            content = "Default mock content"

        return InferenceResponse(
            content=content,
            model_id=request.model_id,
            finish_reason="stop",
            tokens_used=150,
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


@pytest.mark.asyncio
async def test_orchestrator_continuity_warnings_injection(tmp_path):
    # Setup Provider, Model Registry, Mapper
    provider_registry = ProviderRegistry(config=None)
    mock_provider = MockLineageModelProvider()
    provider_registry.register("mock-lineage-provider", mock_provider)

    model_registry = ModelCapabilityRegistry(scanner=None)
    adv_model = ModelDescriptor(
        id="advanced-brain",
        provider="mock-lineage-provider",
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

    # Initialize Orchestrator with our temp DB path
    db_file = tmp_path / "test_lineage_orchestrator.db"
    orchestrator = CouncilOrchestrator(
        provider_registry=provider_registry,
        mapper=mapper,
        historical_accuracy=0.9,
        lineage_db_path=db_file,
    )

    # Pre-populate lineage DB with a past decision and a failure!
    orchestrator.lineage_memory.log_decision(
        decision_id="DEC-555",
        target_subsystem="concurrency_locks",
        rationale="Switched database lock to asynchronous write queue to handle Windows file contention",
        architectural_impact=0.9,
        consequences="Approved",
        alternatives=[{"option_name": "Async Write Queue", "tradeoffs": {}, "rejected_reason": ""}]
    )
    orchestrator.lineage_memory.log_failed_experiment(
        target_subsystem="concurrency_locks",
        patch="conn.execute('BEGIN IMMEDIATE')",
        error_type="sqlite3.OperationalError",
        error_message="database is locked"
    )

    # Force queue processing finish
    orchestrator.lineage_memory.write_queue.join()

    # Trigger a task prompt that matches "concurrency" and "database"
    prompt = "Configure connection pooling with high concurrency locks"
    repo_context = "class ConnectionPool: pass"

    result = await orchestrator.execute_task(prompt, repo_context)

    # 1. Assert that warnings were successfully injected into planner/coder contexts!
    last_planner_prompt = mock_provider.last_planner_prompt
    assert "--- COGNITIVE CONTINUITY WARNINGS ---" in last_planner_prompt
    assert "[DLS] Approved Architectural Decisions:" in last_planner_prompt
    assert "DEC-555" in last_planner_prompt
    assert "Switched database lock" in last_planner_prompt
    assert "[FEL] BLOCK: Prior Failed Experiments" in last_planner_prompt
    assert "database is locked" in last_planner_prompt

    # 2. Assert that this successful run logged a new decision into the DLS!
    orchestrator.lineage_memory.write_queue.join()
    decisions = orchestrator.lineage_memory.get_subsystem_decisions("concurrency")
    
    # We should have 2 decisions now (DEC-555 + the new run DEC)
    assert len(decisions) >= 2
    assert any(d["target_subsystem"] == "concurrency" and d["id"] != "DEC-555" for d in decisions)

    # Graceful shutdown
    orchestrator.lineage_memory.shutdown()
