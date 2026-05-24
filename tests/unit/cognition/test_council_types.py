import pytest
from velune.cognition.council.reviewer import ReviewerAgent
from velune.cognition.council.challenger import ChallengerAgent
from velune.cognition.council.messages import ReviewerMessage, ChallengerMessage, CriticMessage
from velune.cognition.arbitrator import CouncilArbitrator
from velune.cli.display.council_view import CouncilDisplayView
from velune.core.types.model import ModelDescriptor, ModelCapabilityProfile, CapabilityLevel
from velune.providers.base import ModelProvider
from velune.core.types.inference import InferenceRequest, InferenceResponse
from rich.console import Console

class MockAuditProvider(ModelProvider):
    def __init__(self, mock_response: str) -> None:
        self.mock_response = mock_response

    @property
    def provider_id(self) -> str:
        return "mock"

    async def list_models(self) -> list:
        return []

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        return InferenceResponse(
            content=self.mock_response,
            model_id=request.model_id,
            finish_reason="stop",
            tokens_used=10,
            latency_ms=1.5,
        )

@pytest.mark.asyncio
async def test_reviewer_agent_valid_json():
    model = ModelDescriptor(
        id="mock-reviewer",
        provider="mock",
        name="Mock Reviewer",
        context_window=4096,
        is_local=False,
        capabilities=ModelCapabilityProfile(coding=CapabilityLevel.ADVANCED),
        speed_tier="fast",
    )
    # Return valid JSON
    provider = MockAuditProvider('```json\n{"passed": true, "critical_issues": [], "suggestions": ["All clean"], "confidence_rating": 0.9}\n```')
    agent = ReviewerAgent(model=model, provider=provider)
    
    result = await agent.review("task", "proposal", "context")
    assert isinstance(result, ReviewerMessage)
    assert result.passed is True
    assert result.confidence_rating == 0.9
    assert result.parse_error is None

@pytest.mark.asyncio
async def test_reviewer_agent_malformed_json():
    model = ModelDescriptor(
        id="mock-reviewer",
        provider="mock",
        name="Mock Reviewer",
        context_window=4096,
        is_local=False,
        capabilities=ModelCapabilityProfile(coding=CapabilityLevel.ADVANCED),
        speed_tier="fast",
    )
    # Return malformed JSON
    provider = MockAuditProvider("This is not JSON")
    agent = ReviewerAgent(model=model, provider=provider)
    
    result = await agent.review("task", "proposal", "context")
    assert isinstance(result, ReviewerMessage)
    assert result.parse_error is not None
    # Degraded defaults
    assert result.passed is True

def test_council_arbitrator_with_typed_messages():
    arbitrator = CouncilArbitrator()
    
    reviewer = ReviewerMessage(passed=True, critical_issues=[], suggestions=[], confidence_rating=0.9)
    challenger = ChallengerMessage(assumptions_challenged=[], failure_vectors=[], severity_rating=0.1)
    
    res = arbitrator.arbitrate(
        plan_steps=["Step 1"],
        coder_proposal="Some Code",
        reviewer_report=reviewer,
        challenger_report=challenger,
    )
    
    assert res.requires_human_review is False
    assert res.overall_confidence > 0.7

def test_council_display_view_supports_typed_and_dict():
    console = Console()
    view = CouncilDisplayView(console)
    
    reviewer_msg = ReviewerMessage(passed=False, critical_issues=["Bug!"], suggestions=[], confidence_rating=0.8)
    
    # Try rendering with typed message
    view.render_reviewer_report(reviewer_msg)
    
    # Try rendering with model_dump() dict
    view.render_reviewer_report(reviewer_msg.model_dump())
