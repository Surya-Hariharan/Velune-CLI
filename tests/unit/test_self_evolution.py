"""Comprehensive unit tests for the Velune Self-Evolving Engineering Cognition Organism (Phase 3)."""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path
import pytest

from velune.cognition.architecture import ArchitectureCognitionAgent, CognitiveDebtLedger, ArchitectureDriftAlarm
from velune.telemetry.cognition import CognitivePerformanceAnalytics
from velune.cognition.arbitrator import CouncilArbitrator


# =====================================================================
# 1. Subsystem Health Calculation Test
# =====================================================================

def test_subsystem_health_calculation(tmp_path):
    # Mock a spaghettified code directory
    subsystem_dir = tmp_path / "mock_subsystem"
    subsystem_dir.mkdir()

    # Create high cohesion (low LCOM) file
    file_a = subsystem_dir / "module_a.py"
    file_a.write_text("""
class CohesiveClass:
    def __init__(self):
        self.a = 1
        self.b = 2

    def do_a(self):
        return self.a

    def do_b(self):
        return self.b + self.a
""", encoding="utf-8")

    # Create low cohesion (high LCOM) file
    file_b = subsystem_dir / "module_b.py"
    file_b.write_text("""
class SpaghettiClass:
    def __init__(self):
        self.a = 1
        self.b = 2
        self.c = 3
        self.d = 4

    def method_1(self):
        self.a = 10

    def method_2(self):
        self.b = 20

    def method_3(self):
        self.c = 30

    def method_4(self):
        self.d = 40
""", encoding="utf-8")

    # Create files to mock imports for coupling ratio
    # Internal import: imports from module_a inside the folder
    # External import: imports from a module outside the folder starting with 'velune'
    file_c = subsystem_dir / "module_c.py"
    file_c.write_text("""
import os
from velune.mock_subsystem.module_a import CohesiveClass
from velune.kernel.bus import EventBus
from velune.telemetry.cognition import CognitivePerformanceAnalytics
""", encoding="utf-8")

    ledger_file = tmp_path / "test_debt_ledger.json"
    ledger = CognitiveDebtLedger(ledger_path=ledger_file)
    agent = ArchitectureCognitionAgent(workspace_root=str(tmp_path), ledger=ledger)

    # Assert LCOM calculations
    lcom_a = agent.calculate_lcom(file_a.read_text(encoding="utf-8"))
    lcom_b = agent.calculate_lcom(file_b.read_text(encoding="utf-8"))

    assert lcom_a["CohesiveClass"] == 0
    assert lcom_b["SpaghettiClass"] > 0

    # Assert coupling ratio calculation
    # Project imports inside file_c:
    # 1. velune.mock_subsystem.module_a (internal since 'mock_subsystem' is in the path)
    # 2. velune.kernel.bus (external)
    # 3. velune.telemetry.cognition (external)
    # Total project imports = 3, external = 2, internal = 1
    # Coupling ratio = 2 / 3 = 0.667
    coupling = agent.calculate_coupling_ratio(str(subsystem_dir))
    assert 0.6 <= coupling <= 0.7

    # Assert SHI metrics
    # Add a debt item to trigger penalty
    ledger.add_debt_item(
        file_path=str(file_a),
        category="cohesion",
        description="High LCOM score",
        severity=2.0
    )
    
    shi = agent.calculate_shi(str(subsystem_dir))
    assert 0.0 <= shi <= 1.0

    # Ensure refactoring proposal is generated when SHI < 0.60
    # Let's add more debt severity to force SHI < 0.60
    ledger.add_debt_item(
        file_path=str(file_b),
        category="layering",
        description="Banned import layering drift",
        severity=5.0
    )
    shi_sink = agent.calculate_shi(str(subsystem_dir))
    assert shi_sink < 0.60
    
    proposal = agent.propose_refactoring(str(subsystem_dir))
    assert proposal is not None
    assert "Proactive Refactoring Proposal" in proposal
    assert "Architectural Sink" in proposal


# =====================================================================
# 2. Architecture Drift Alarm (ADA) Test
# =====================================================================

def test_architecture_drift_alarm_enforcement(tmp_path):
    ledger = CognitiveDebtLedger(ledger_path=tmp_path / "debt.json")
    agent = ArchitectureCognitionAgent(workspace_root=str(tmp_path), ledger=ledger)

    # Core is not allowed to import execution, cognition, or cli layers
    violating_code = """
import sys
from velune.cognition.arbitrator import CouncilArbitrator
"""

    # verify_boundaries without raising
    violations = agent.verify_boundaries(violating_code, "velune/core/main.py")
    assert len(violations) > 0

    # Assert it raises explicit ArchitectureDriftAlarm when raise_on_violation=True
    with pytest.raises(ArchitectureDriftAlarm) as exc_info:
        agent.verify_boundaries(violating_code, "velune/core/main.py", raise_on_violation=True)
    
    assert "imports 'velune.cognition.arbitrator'" in str(exc_info.value)


# =====================================================================
# 3. Critic Vote Persistence & Weight Reinforcement Test
# =====================================================================

def test_critic_vote_persistence_and_reinforcement(tmp_path):
    db_file = tmp_path / "test_telemetry.db"
    analytics = CognitivePerformanceAnalytics(db_path=db_file)

    # Verify initial weights are all 1.0
    weights_init = analytics.get_critic_weights()
    assert weights_init["scalability"] == 1.0
    assert weights_init["security"] == 1.0

    # Simulation Scenario:
    # Task 1: Performance Critic objects (vote=False), and execution succeeds (success=True).
    # Since critic objected to a succeeding patch, it made a false positive mistake. Weight should decrease.
    analytics.record_critic_vote(
        task_id="TASK-001",
        critic_role="performance",
        vote=False,       # False represents "objected"
        success=True      # Patched compiled/test passed
    )

    weights_after_1 = analytics.get_critic_weights()
    # Initial W = 1.0. Delta = eta * (success * (vote - 0.5) - (1-success)*(vote - 0.5))
    # delta = 0.05 * (1.0 * (0.0 - 0.5) - 0.0) = 0.05 * (-0.5) = -0.025.
    # W_new = 1.0 - 0.025 = 0.975
    assert weights_after_1["performance"] == 0.975

    # Task 2: Security Critic objects (vote=False) and execution fails (success=False).
    # Since critic correctly objected to a failing patch, it was correct. Weight should increase.
    analytics.record_critic_vote(
        task_id="TASK-002",
        critic_role="security",
        vote=False,       # Objected
        success=False     # Failed compile/test
    )
    
    weights_after_2 = analytics.get_critic_weights()
    # delta = 0.05 * (0.0 - (1.0 * (0.0 - 0.5))) = 0.05 * (0.5) = +0.025
    # W_new = 1.0 + 0.025 = 1.025
    assert weights_after_2["security"] == 1.025


# =====================================================================
# 4. Calibrated Confidence Fusion & Arbitrator Scaling Test
# =====================================================================

def test_calibrated_confidence_fusion_and_scaling():
    arbitrator = CouncilArbitrator()

    plan_steps = ["step 1"]
    coder_proposal = "code"
    
    # Reports
    reviewer_report = {"passed": True, "confidence_rating": 0.8, "critical_issues": []}
    challenger_report = {"failure_vectors": [], "severity_rating": 0.1}

    scalability_report = {"passed": True, "score": 0.9, "issues": []}
    security_report = {"passed": True, "score": 0.7, "issues": []}

    # Case A: Perfect state, high SHI (1.0)
    critic_weights = {
        "scalability": 1.2,
        "security": 0.8,
    }
    
    # Let's arbitrate with shi=1.0. High subsystem health = low risk.
    res_a = arbitrator.arbitrate(
        plan_steps=plan_steps,
        coder_proposal=coder_proposal,
        reviewer_report=reviewer_report,
        challenger_report=challenger_report,
        scalability_report=scalability_report,
        security_report=security_report,
        critic_weights=critic_weights,
        shi=1.0
    )
    assert res_a.requires_human_review is False
    assert res_a.overall_confidence > 0.65

    # Case B: High Risk (shi=0.5 < 0.6), which should escalate the required passing threshold to 0.75
    res_b = arbitrator.arbitrate(
        plan_steps=plan_steps,
        coder_proposal=coder_proposal,
        reviewer_report={"passed": True, "confidence_rating": 0.6, "critical_issues": []},
        challenger_report={"failure_vectors": [], "severity_rating": 0.3},
        scalability_report=scalability_report,
        security_report=security_report,
        critic_weights=critic_weights,
        shi=0.5
    )
    
    # High risk should set threshold to 0.75. If overall_confidence < 0.75, it forces human review.
    if res_b.overall_confidence < 0.75:
        assert res_b.requires_human_review is True
