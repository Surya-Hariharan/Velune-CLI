import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from velune.cognition.orchestrator import CouncilOrchestrator
from velune.models.specializations import CouncilRole


class MockModelDescriptor:
    def __init__(self, provider_id: str, model_id: str):
        self.provider_id = provider_id
        self.model_id = model_id
        self.is_local = False
        self.vram_required_gb = None


class MockModelSpecializationMapper:
    def __init__(self, model_id: str = "test-model-id"):
        self.model_id = model_id
        self.profiler = MagicMock()
        self.profiler.get_profile.return_value = None

    def map_roles(self, *args, **kwargs):
        desc = MockModelDescriptor("mock-provider", self.model_id)
        return {
            CouncilRole.PLANNER: desc,
            CouncilRole.CODER: desc,
            CouncilRole.REVIEWER: desc,
            CouncilRole.CHALLENGER: desc,
            CouncilRole.SYNTHESIZER: desc,
        }


class MockProviderRegistry:
    def __init__(self, provider):
        self.provider = provider

    def get_or_raise(self, name):
        return self.provider


class MockProvider:
    def __init__(self, slow=False):
        self.slow = slow

    async def infer(self, request):
        if self.slow:
            await asyncio.sleep(5.0)

        mock_response = MagicMock()
        content = request.messages[-1]["content"]
        if (
            "Respond with ONLY a JSON array" in content
            or "Respond with ONLY a JSON list" in content
            or "json" in request.messages[0]["content"].lower()
        ):
            mock_response.content = '[{"id": "step-1", "description": "Write code", "agent_role": "coder", "dependencies": []}]'
        elif (
            "ReviewerMessage" in str(request.messages)
            or "review" in request.messages[0]["content"].lower()
        ):
            mock_response.content = '{"passed": true, "critical_issues": [], "suggestions": [], "confidence_rating": 0.9}'
        elif (
            "ChallengerMessage" in str(request.messages)
            or "challenge" in request.messages[0]["content"].lower()
        ):
            mock_response.content = (
                '{"assumptions_challenged": [], "failure_vectors": [], "severity_rating": 0.0}'
            )
        elif (
            "CriticMessage" in str(request.messages)
            or "critique" in request.messages[0]["content"].lower()
        ):
            mock_response.content = (
                '{"passed": true, "issues": [], "score": 0.95, "rationale": "Perfect"}'
            )
        else:
            mock_response.content = "Mock response implementation code"
        return mock_response


def build_test_orchestrator(slow_providers=False):
    provider = MockProvider(slow=slow_providers)
    registry = MockProviderRegistry(provider)
    mapper = MockModelSpecializationMapper()

    orchestrator = CouncilOrchestrator(
        provider_registry=registry,
        mapper=mapper,
        lineage_db_path=None,
    )
    orchestrator.lineage_memory = MagicMock()
    orchestrator.lineage_memory.get_personality_style.return_value = None
    # query_continuity_warnings and log_decision/log_failed_experiment are awaited in the
    # orchestrator, so they must be AsyncMocks.
    orchestrator.lineage_memory.query_continuity_warnings = AsyncMock(return_value=([], []))
    orchestrator.lineage_memory.log_decision = AsyncMock()
    orchestrator.lineage_memory.log_failed_experiment = AsyncMock()

    return orchestrator


@pytest.mark.asyncio
async def test_council_respects_wall_time_limit(monkeypatch):
    """Council must return within wall time even if agents are slow."""
    monkeypatch.setenv("VELUNE_COUNCIL_MAX_SECONDS", "2")

    orchestrator = build_test_orchestrator(slow_providers=True)

    start = time.time()
    result = await orchestrator.execute_task("fix auth bug", "context here")
    elapsed = time.time() - start

    assert elapsed < 4.0, f"Wall time exceeded: {elapsed:.1f}s"
    assert result["arbitration"]["flags"] == ["TIMEOUT"]
    assert result["arbitration"]["requires_human_review"] is True


@pytest.mark.asyncio
async def test_progress_callback_called():
    """Progress callback must be called at each phase."""
    progress_events = []
    orchestrator = build_test_orchestrator()

    await orchestrator.execute_task(
        "fix auth bug", "context", council_tier="instant", progress_callback=progress_events.append
    )

    assert len(progress_events) > 0
    assert any("Planner" in e or "Coder" in e for e in progress_events)


@pytest.mark.asyncio
async def test_council_graceful_degradation_on_exception(monkeypatch):
    """Council must complete successfully with degraded defaults when model is offline mid-execution."""
    from velune.cognition.council.reviewer import ReviewerAgent
    from velune.cognition.council.synthesizer import SynthesizerAgent

    monkeypatch.setattr(
        ReviewerAgent,
        "review",
        AsyncMock(side_effect=RuntimeError("Reviewer model went offline mid-deliberation!")),
    )
    monkeypatch.setattr(
        SynthesizerAgent,
        "synthesize",
        AsyncMock(side_effect=RuntimeError("Synthesizer model went offline mid-synthesis!")),
    )

    class MockFailingProvider:
        async def infer(self, request):
            system_prompt = request.messages[0]["content"] if request.messages else ""
            mock_response = MagicMock()
            if (
                "Respond with ONLY a JSON array" in request.messages[-1]["content"]
                or "json" in system_prompt.lower()
            ):
                if "Adversarial Challenger" in system_prompt:
                    mock_response.content = '{"assumptions_challenged": [], "failure_vectors": [], "severity_rating": 0.0}'
                elif "Lead Planner" in system_prompt:
                    mock_response.content = '{"task_id": "task-main", "steps": [{"id": "step-1", "description": "Write code", "agent_role": "coder", "dependencies": []}]}'
                elif (
                    "Critic" in system_prompt
                    or "Security" in system_prompt
                    or "Scalability" in system_prompt
                    or "Performance" in system_prompt
                    or "Maintainability" in system_prompt
                ):
                    mock_response.content = (
                        '{"passed": true, "issues": [], "score": 0.95, "rationale": "Perfect"}'
                    )
                else:
                    mock_response.content = '{"passed": true, "critical_issues": [], "suggestions": [], "confidence_rating": 0.9}'
            else:
                mock_response.content = "Mock fallback response code"
            return mock_response

    provider = MockFailingProvider()
    registry = MockProviderRegistry(provider)
    MockModelSpecializationMapper()

    orchestrator = build_test_orchestrator()
    orchestrator.provider_registry = registry

    result = await orchestrator.execute_task(
        prompt="upgrade database", repo_context="some context", council_tier="full"
    )

    assert result["tier"] == "full"
    assert result["reviewer_report"] is not None
    assert result["reviewer_report"].passed is True
    assert result["reviewer_report"].critical_issues == ["Reviewer unavailable"]
    assert result["reviewer_report"].confidence_rating == 0.5
    assert result["arbitration"] is not None
    assert result["arbitration"]["overall_confidence"] < 0.95
    assert result["final_summary"] is not None
    assert (
        "unavailable" in result["final_summary"].lower()
        or "degraded" in result["final_summary"].lower()
    )
