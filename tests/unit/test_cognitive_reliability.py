"""Comprehensive unit tests for the Velune Cognitive Reliability Architecture."""

from __future__ import annotations

import os
import shutil
import time
import pytest
import sqlite3
import json
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

from velune.orchestration.checkpoints import SQLiteCheckpointStore
from velune.orchestration.schemas import OrchestrationState, OrchestrationRequest, ExecutionStatus
from velune.context.scorer import ContextAttentionPrioritizer, ContextScorer
from velune.cognition.verification import ReasoningVerifier
from velune.memory.tiers.graph import GraphMemoryTier
from velune.repository.watcher import WorkspaceEvolutionWatcher
from velune.telemetry.cognition import CognitivePerformanceAnalytics
from velune.cognition.arbitrator import CouncilArbitrator


# =====================================================================
# 1. Component 6: Persistent Checkpoint Store Tests
# =====================================================================

def test_sqlite_checkpoint_store(tmp_path):
    db_file = tmp_path / "test_checkpoints.db"
    store = SQLiteCheckpointStore(db_path=db_file)
    
    # Create mock OrchestrationState
    req = OrchestrationRequest(
        prompt="Optimizing backend performance",
        workspace="c:\\src",
        task_id="task-123",
        model="gpt-4",
    )
    state = OrchestrationState(
        run_id="run-999",
        request=req,
        status=ExecutionStatus.PENDING,
    )
    
    # Save checkpoint
    cp_id = store.save(run_id="run-999", node_name="plan_node", state=state)
    assert cp_id == "run-999:cp:0001"
    
    # Check count
    ids = store.list_ids(run_id="run-999")
    assert len(ids) == 1
    assert ids[0] == "run-999:cp:0001"
    
    # Save second checkpoint
    cp_id2 = store.save(run_id="run-999", node_name="execute_node", state=state)
    assert cp_id2 == "run-999:cp:0002"
    
    # Load latest checkpoint
    latest_state = store.latest(run_id="run-999")
    assert latest_state is not None
    assert latest_state.run_id == "run-999"
    assert latest_state.request.prompt == "Optimizing backend performance"
    
    # Load specific checkpoint
    specific_state = store.load(run_id="run-999", checkpoint_id="run-999:cp:0001")
    assert specific_state is not None
    assert specific_state.run_id == "run-999"


# =====================================================================
# 2. Component 1: Context Attention Prioritizer & Scorer Tests
# =====================================================================

def test_context_attention_prioritizer_and_scorer():
    prioritizer = ContextAttentionPrioritizer()
    scorer = ContextScorer(prioritizer=prioritizer)
    
    now = time.time()
    
    # Item 1: Normal source code
    item_source = {
        "semantic_score": 0.8,
        "timestamp": now,
        "connections": 0,
        "context_type": "source_code",
        "is_unresolved_task": False,
        "is_core_dependency": False,
    }
    
    # Item 2: Unresolved task (should be boosted)
    item_task = {
        "semantic_score": 0.5,
        "timestamp": now,
        "connections": 0,
        "context_type": "task",
        "is_unresolved_task": True,
        "is_core_dependency": False,
    }
    
    # Item 3: Core dependency
    item_dep = {
        "semantic_score": 0.6,
        "timestamp": now,
        "connections": 5,
        "context_type": "source_code",
        "is_unresolved_task": False,
        "is_core_dependency": True,
    }
    
    ranked = scorer.rank_items([item_source, item_task, item_dep])
    
    assert len(ranked) == 3
    # Check that unresolved task gets boosted and ranked high
    assert ranked[0]["is_unresolved_task"] is True or ranked[0]["is_core_dependency"] is True


# =====================================================================
# 3. Component 2: Self-Reflection & Reasoning Verification Tests
# =====================================================================

def test_reasoning_verifier_imports_and_contradictions(tmp_path):
    verifier = ReasoningVerifier(workspace_root=str(tmp_path))
    
    # Safe Imports
    code_safe = "import sys\nimport os"
    results = verifier.analyze_proposed_imports(code_safe)
    assert results["success"] is True
    assert len(results["issues"]) == 0
    
    # Hallucinated Import
    code_unsafe = "import nonexistent_hallucinated_module"
    results_unsafe = verifier.analyze_proposed_imports(code_unsafe)
    assert results_unsafe["success"] is False
    assert any("Potential hallucinated import" in issue for issue in results_unsafe["issues"])
    
    # Signature Contradiction Detection
    existing_code = """
def update_profile(user_id: int, username: str) -> None:
    pass
"""
    proposed_code_diff = """
def update_profile(user_id: int) -> None: # Missing argument!
    pass
"""
    contra_results = verifier.analyze_contradictions(proposed_code_diff, existing_code)
    assert contra_results["success"] is False
    assert any("Signature mismatch" in issue for issue in contra_results["issues"])


# =====================================================================
# 4. Component 3: Execution Lineage Graph (DAG) Tests
# =====================================================================

def test_execution_lineage_graph_dag(tmp_path):
    db_file = tmp_path / "test_graph.db"
    graph = GraphMemoryTier(db_path=db_file)
    
    # Record nodes
    graph.record_execution_node(
        node_id="exec-001",
        task_id="task-abc",
        node_type="action",
        status="failed",
        parameters={"file": "server.py", "action": "modify"},
        outcome="AssertionError",
    )
    
    graph.record_execution_node(
        node_id="exec-002",
        task_id="task-abc",
        node_type="rollback",
        status="completed",
        parameters={"file": "server.py", "action": "rollback"},
        outcome="Success",
    )
    
    # Record edge
    graph.record_execution_edge(
        source_id="exec-001",
        target_id="exec-002",
        relation_type="rolled_back_by",
    )
    
    # Query lineage
    lineage = graph.query_execution_lineage("server.py")
    assert len(lineage) == 2
    assert lineage[0]["id"] in ("exec-001", "exec-002")


# =====================================================================
# 5. Component 4: Workspace Evolution Watcher Tests
# =====================================================================

def test_workspace_evolution_watcher_polling(tmp_path):
    # Setup mock indexer and files
    indexer = MagicMock()
    indexer.cache_path = tmp_path / "index_cache.json"
    indexer._compute_sha256.return_value = "dummy-sha"
    indexer.parser = MagicMock()
    indexer.parser.parse.return_value = ([], [])
    indexer.parser._detect_language.return_value = MagicMock(value="python")
    
    test_file = tmp_path / "test.py"
    test_file.write_text("print('hello')", encoding="utf-8")
    
    watcher = WorkspaceEvolutionWatcher(
        root_path=tmp_path,
        indexer=indexer,
        poll_interval=0.1,
    )
    
    # Scan initial state
    watcher._scan_initial_states()
    assert "test.py" in watcher._file_states
    
    # Simulate modified event
    test_file.write_text("print('hello world')", encoding="utf-8")
    
    # Directly trigger event handler
    watcher._handle_file_event(test_file, "modified")
    assert indexer.parser.parse.called


# =====================================================================
# 6. Component 8: Cognitive Performance Analytics & Routing Tests
# =====================================================================

def test_cognitive_performance_analytics_and_routing(tmp_path):
    db_file = tmp_path / "test_analytics.db"
    analytics = CognitivePerformanceAnalytics(db_path=db_file)
    
    # Record some performance metrics
    analytics.record_metrics(
        model_id="gemini-flash",
        task_type="coding",
        hallucinated=False,
        rolled_back=False,
        token_count=1500,
        execution_time_ms=800,
        success=True,
    )
    
    analytics.record_metrics(
        model_id="gemini-flash",
        task_type="coding",
        hallucinated=True,
        rolled_back=True,
        token_count=1800,
        execution_time_ms=1200,
        success=False,
    )
    
    analytics.record_metrics(
        model_id="claude-sonnet",
        task_type="coding",
        hallucinated=False,
        rolled_back=False,
        token_count=2000,
        execution_time_ms=1500,
        success=True,
    )
    
    # Get model performance
    flash_perf = analytics.get_model_performance("gemini-flash")
    assert flash_perf["total_runs"] == 2
    assert flash_perf["success_rate"] == 0.5
    assert flash_perf["hallucination_rate"] == 0.5
    
    sonnet_perf = analytics.get_model_performance("claude-sonnet")
    assert sonnet_perf["total_runs"] == 1
    assert sonnet_perf["success_rate"] == 1.0
    
    # Route task: claude-sonnet should be selected over gemini-flash because of higher success and lower rollback/hallucination
    best_model = analytics.route_reasoning_task(
        task_type="coding",
        available_models=["gemini-flash", "claude-sonnet"],
    )
    assert best_model == "claude-sonnet"
