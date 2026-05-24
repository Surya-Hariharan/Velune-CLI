import pytest
import tempfile
import pathlib
import time
from unittest.mock import MagicMock, AsyncMock

from velune.cognition.council.debate import DebateConfig, calculate_max_debate_turns
from velune.telemetry.cognition import CognitivePerformanceAnalytics
from velune.cognition.orchestrator import CouncilOrchestrator
from velune.models.specializations import ModelSpecializationMapper, CouncilRole
from velune.providers.registry import ProviderRegistry
from velune.core.types.model import ModelDescriptor, ModelCapabilityProfile, CapabilityLevel
from velune.cognition.council.messages import ReviewerMessage, ChallengerMessage, CriticMessage
from velune.cognition.arbitrator import CouncilArbitrator, ArbitrationResult
from velune.core.types.task import TaskPlan, TaskStep

# =====================================================================
# 1. calculate_max_debate_turns Tests
# =====================================================================

def test_calculate_max_debate_turns_zero_objections():
    turns = calculate_max_debate_turns(
        initial_objections=[],
        critic_reports={},
        task_complexity="structural"
    )
    assert turns == 0

def test_calculate_max_debate_turns_security_failure():
    turns = calculate_max_debate_turns(
        initial_objections=["Security issues"],
        critic_reports={"security": {"passed": False}},
        task_complexity="structural"
    )
    assert turns >= 4

def test_calculate_max_debate_turns_simple_task():
    turns = calculate_max_debate_turns(
        initial_objections=["One objection"],
        critic_reports={},
        task_complexity="simple"
    )
    assert turns == 2

def test_calculate_max_debate_turns_challenger_severity():
    turns = calculate_max_debate_turns(
        initial_objections=["Objection"],
        critic_reports={"challenger": {"severity_rating": 0.9}},
        task_complexity="structural"
    )
    assert turns == 4  # base_max(3) + 1 boost

def test_calculate_max_debate_turns_hard_cap():
    turns = calculate_max_debate_turns(
        initial_objections=["Objection"],
        critic_reports={
            "security": {"passed": False},
            "challenger": {"severity_rating": 0.9}
        },
        task_complexity="structural"
    )
    assert turns == 5

# =====================================================================
# 2. debate_telemetry Recording Tests
# =====================================================================

def test_debate_telemetry_recording():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_path = pathlib.Path(tmpdir) / "test_telemetry.db"
        analytics = CognitivePerformanceAnalytics(db_path=db_path)
        
        # Table should exist and be empty
        with analytics._get_connection() as conn:
            row = conn.execute("SELECT COUNT(*) as cnt FROM debate_telemetry").fetchone()
            assert row["cnt"] == 0
        
        # Record a debate outcome
        analytics.record_debate_outcome(
            turns_required=3,
            initial_objection_count=2,
            final_objection_count=1,
            converged=False,
            time_to_converge_ms=350
        )
        
        # Verify persistence
        with analytics._get_connection() as conn:
            row = conn.execute("SELECT * FROM debate_telemetry").fetchone()
            assert row["turns_required"] == 3
            assert row["initial_objection_count"] == 2
            assert row["final_objection_count"] == 1
            assert row["converged"] == 0
            assert row["time_to_converge_ms"] == 350

# =====================================================================
# 3. Orchestrator Integration & Flow Tests
# =====================================================================

@pytest.fixture
def mock_models_setup():
    provider_registry = MagicMock(spec=ProviderRegistry)
    mapper = MagicMock(spec=ModelSpecializationMapper)
    
    mock_model = ModelDescriptor(
        id="test-model",
        provider="mock",
        name="Test Model",
        context_window=4096,
        is_local=False,
        capabilities=ModelCapabilityProfile(coding=CapabilityLevel.ADVANCED),
        speed_tier="fast"
    )
    mapper.map_roles.return_value = {
        CouncilRole.PLANNER: mock_model,
        CouncilRole.CODER: mock_model,
        CouncilRole.REVIEWER: mock_model,
        CouncilRole.CHALLENGER: mock_model,
        CouncilRole.SYNTHESIZER: mock_model,
    }
    return provider_registry, mapper

@pytest.mark.asyncio
async def test_orchestrator_debate_zero_objections_flow(mock_models_setup):
    provider_registry, mapper = mock_models_setup
    
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_path = pathlib.Path(tmpdir) / "test_lineage.db"
        analytics_db = pathlib.Path(tmpdir) / "test_analytics.db"
        
        analytics = CognitivePerformanceAnalytics(db_path=analytics_db)
        orchestrator = CouncilOrchestrator(
            provider_registry=provider_registry,
            mapper=mapper,
            lineage_db_path=db_path,
            analytics=analytics
        )
        
        # Mock agents and critics
        orchestrator._is_structural_change = MagicMock(return_value=True)
        orchestrator._get_or_refresh_style_profile = MagicMock(return_value={})
        
        # Mock Planner producing structured plan
        mock_planner = AsyncMock()
        mock_planner.generate_plan.return_value = TaskPlan(
            task_id="task-main",
            steps=[TaskStep(id="step-1", description="Do something", agent_role="coder")]
        )
        
        # Mock Coder
        mock_coder = AsyncMock()
        mock_coder.write_code.return_value = "def my_func(): pass"
        
        # Mock Reviewer & Challenger (All pass!)
        mock_reviewer = AsyncMock()
        mock_reviewer.review.return_value = ReviewerMessage(passed=True, confidence_rating=0.9, critical_issues=[])
        
        mock_challenger = AsyncMock()
        mock_challenger.challenge.return_value = ChallengerMessage(severity_rating=0.1, failure_vectors=[])
        
        # Mock Critics (All pass!)
        mock_critic = AsyncMock()
        mock_critic.critique.return_value = CriticMessage(passed=True, score=0.9)
        
        # Mock Synthesizer
        mock_synthesizer = AsyncMock()
        mock_synthesizer.synthesize.return_value = "Synthesized design and execution layout."
        
        import velune.cognition.orchestrator as orch
        orig_planner = orch.PlannerAgent
        orig_coder = orch.CoderAgent
        orig_reviewer = orch.ReviewerAgent
        orig_challenger = orch.ChallengerAgent
        orig_sc = orch.ScalabilityCritic
        orig_sec = orch.SecurityCritic
        orig_perf = orch.PerformanceCritic
        orig_mc = orch.MaintainabilityCritic
        orig_synthesizer = orch.SynthesizerAgent
        
        orch.PlannerAgent = MagicMock(return_value=mock_planner)
        orch.CoderAgent = MagicMock(return_value=mock_coder)
        orch.ReviewerAgent = MagicMock(return_value=mock_reviewer)
        orch.ChallengerAgent = MagicMock(return_value=mock_challenger)
        orch.ScalabilityCritic = MagicMock(return_value=mock_critic)
        orch.SecurityCritic = MagicMock(return_value=mock_critic)
        orch.PerformanceCritic = MagicMock(return_value=mock_critic)
        orch.MaintainabilityCritic = MagicMock(return_value=mock_critic)
        orch.SynthesizerAgent = MagicMock(return_value=mock_synthesizer)
        
        try:
            result = await orchestrator.execute_task("Build class and database", "code here")
            
            # Verify no debate outcome recorded because objections was 0
            with analytics._get_connection() as conn:
                cnt = conn.execute("SELECT COUNT(*) as cnt FROM debate_telemetry").fetchone()["cnt"]
                assert cnt == 0
                
        finally:
            orchestrator.lineage_memory.shutdown()
            
            # Restore
            orch.PlannerAgent = orig_planner
            orch.CoderAgent = orig_coder
            orch.ReviewerAgent = orig_reviewer
            orch.ChallengerAgent = orig_challenger
            orch.ScalabilityCritic = orig_sc
            orch.SecurityCritic = orig_sec
            orch.PerformanceCritic = orig_perf
            orch.MaintainabilityCritic = orig_mc
            orch.SynthesizerAgent = orig_synthesizer

@pytest.mark.asyncio
async def test_orchestrator_debate_with_early_convergence_flow(mock_models_setup):
    provider_registry, mapper = mock_models_setup
    
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_path = pathlib.Path(tmpdir) / "test_lineage.db"
        analytics_db = pathlib.Path(tmpdir) / "test_analytics.db"
        
        analytics = CognitivePerformanceAnalytics(db_path=analytics_db)
        orchestrator = CouncilOrchestrator(
            provider_registry=provider_registry,
            mapper=mapper,
            lineage_db_path=db_path,
            analytics=analytics
        )
        
        orchestrator._is_structural_change = MagicMock(return_value=True)
        orchestrator._get_or_refresh_style_profile = MagicMock(return_value={})
        
        mock_planner = AsyncMock()
        mock_planner.generate_plan.return_value = TaskPlan(
            task_id="task-main",
            steps=[TaskStep(id="step-1", description="Do something", agent_role="coder")]
        )
        
        mock_coder = AsyncMock()
        mock_coder.write_code.return_value = "def my_func(): pass"
        
        # Initial review fails (objection!)
        mock_reviewer = AsyncMock()
        mock_reviewer.review.side_effect = [
            ReviewerMessage(passed=False, confidence_rating=0.4, critical_issues=["Minor bug"]), # Turn 0
            ReviewerMessage(passed=True, confidence_rating=0.9, critical_issues=[]) # Turn 1
        ]
        
        mock_challenger = AsyncMock()
        mock_challenger.challenge.return_value = ChallengerMessage(severity_rating=0.1, failure_vectors=[])
        
        mock_critic = AsyncMock()
        mock_critic.critique.return_value = CriticMessage(passed=True, score=0.9)
        
        mock_synthesizer = AsyncMock()
        mock_synthesizer.synthesize.return_value = "Synthesized design and execution layout."
        
        import velune.cognition.orchestrator as orch
        orig_planner = orch.PlannerAgent
        orig_coder = orch.CoderAgent
        orig_reviewer = orch.ReviewerAgent
        orig_challenger = orch.ChallengerAgent
        orig_sc = orch.ScalabilityCritic
        orig_sec = orch.SecurityCritic
        orig_perf = orch.PerformanceCritic
        orig_mc = orch.MaintainabilityCritic
        orig_synthesizer = orch.SynthesizerAgent
        
        orch.PlannerAgent = MagicMock(return_value=mock_planner)
        orch.CoderAgent = MagicMock(return_value=mock_coder)
        orch.ReviewerAgent = MagicMock(return_value=mock_reviewer)
        orch.ChallengerAgent = MagicMock(return_value=mock_challenger)
        orch.ScalabilityCritic = MagicMock(return_value=mock_critic)
        orch.SecurityCritic = MagicMock(return_value=mock_critic)
        orch.PerformanceCritic = MagicMock(return_value=mock_critic)
        orch.MaintainabilityCritic = MagicMock(return_value=mock_critic)
        orch.SynthesizerAgent = MagicMock(return_value=mock_synthesizer)
        
        try:
            result = await orchestrator.execute_task("Build class and database", "code here")
            
            # Verify debate outcome recorded
            with analytics._get_connection() as conn:
                row = conn.execute("SELECT * FROM debate_telemetry").fetchone()
                assert row is not None
                assert row["turns_required"] == 1
                assert row["initial_objection_count"] == 1
                assert row["final_objection_count"] == 0
                assert row["converged"] == 1
                
        finally:
            orchestrator.lineage_memory.shutdown()
            
            orch.PlannerAgent = orig_planner
            orch.CoderAgent = orig_coder
            orch.ReviewerAgent = orig_reviewer
            orch.ChallengerAgent = orig_challenger
            orch.ScalabilityCritic = orig_sc
            orch.SecurityCritic = orig_sec
            orch.PerformanceCritic = orig_perf
            orch.MaintainabilityCritic = orig_mc
            orch.SynthesizerAgent = orig_synthesizer

@pytest.mark.asyncio
async def test_orchestrator_debate_security_failure_flow(mock_models_setup):
    provider_registry, mapper = mock_models_setup
    
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_path = pathlib.Path(tmpdir) / "test_lineage.db"
        analytics_db = pathlib.Path(tmpdir) / "test_analytics.db"
        
        analytics = CognitivePerformanceAnalytics(db_path=analytics_db)
        orchestrator = CouncilOrchestrator(
            provider_registry=provider_registry,
            mapper=mapper,
            lineage_db_path=db_path,
            analytics=analytics
        )
        
        orchestrator._is_structural_change = MagicMock(return_value=True)
        orchestrator._get_or_refresh_style_profile = MagicMock(return_value={})
        
        mock_planner = AsyncMock()
        mock_planner.generate_plan.return_value = TaskPlan(
            task_id="task-main",
            steps=[TaskStep(id="step-1", description="Do something", agent_role="coder")]
        )
        
        mock_coder = AsyncMock()
        mock_coder.write_code.return_value = "def my_func(): pass"
        
        mock_reviewer = AsyncMock()
        mock_reviewer.review.return_value = ReviewerMessage(passed=True, confidence_rating=0.9, critical_issues=[])
        
        mock_challenger = AsyncMock()
        mock_challenger.challenge.return_value = ChallengerMessage(severity_rating=0.1, failure_vectors=[])
        
        # Security Critic always fails!
        mock_security_critic = AsyncMock()
        mock_security_critic.critique.return_value = CriticMessage(passed=False, score=0.3, issues=["Security flaw!"])
        
        mock_other_critic = AsyncMock()
        mock_other_critic.critique.return_value = CriticMessage(passed=True, score=0.9)
        
        mock_synthesizer = AsyncMock()
        mock_synthesizer.synthesize.return_value = "Synthesized design and execution layout."
        
        import velune.cognition.orchestrator as orch
        orig_planner = orch.PlannerAgent
        orig_coder = orch.CoderAgent
        orig_reviewer = orch.ReviewerAgent
        orig_challenger = orch.ChallengerAgent
        orig_sc = orch.ScalabilityCritic
        orig_sec = orch.SecurityCritic
        orig_perf = orch.PerformanceCritic
        orig_mc = orch.MaintainabilityCritic
        orig_synthesizer = orch.SynthesizerAgent
        
        orch.PlannerAgent = MagicMock(return_value=mock_planner)
        orch.CoderAgent = MagicMock(return_value=mock_coder)
        orch.ReviewerAgent = MagicMock(return_value=mock_reviewer)
        orch.ChallengerAgent = MagicMock(return_value=mock_challenger)
        orch.ScalabilityCritic = MagicMock(return_value=mock_other_critic)
        orch.SecurityCritic = MagicMock(return_value=mock_security_critic)
        orch.PerformanceCritic = MagicMock(return_value=mock_other_critic)
        orch.MaintainabilityCritic = MagicMock(return_value=mock_other_critic)
        orch.SynthesizerAgent = MagicMock(return_value=mock_synthesizer)
        
        try:
            result = await orchestrator.execute_task("Build class and database", "code here")
            
            # Verify debate outcome recorded
            with analytics._get_connection() as conn:
                row = conn.execute("SELECT * FROM debate_telemetry").fetchone()
                assert row is not None
                # Since security fails, max turns is calculated as max(3, 4) -> 4
                assert row["turns_required"] == 4
                assert row["initial_objection_count"] == 1
                assert row["final_objection_count"] == 1
                assert row["converged"] == 0
                
        finally:
            orchestrator.lineage_memory.shutdown()
            
            orch.PlannerAgent = orig_planner
            orch.CoderAgent = orig_coder
            orch.ReviewerAgent = orig_reviewer
            orch.ChallengerAgent = orig_challenger
            orch.ScalabilityCritic = orig_sc
            orch.SecurityCritic = orig_sec
            orch.PerformanceCritic = orig_perf
            orch.MaintainabilityCritic = orig_mc
            orch.SynthesizerAgent = orig_synthesizer
