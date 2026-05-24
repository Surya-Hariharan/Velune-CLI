"""Unit tests for the configurable CriticAgent and its configurations."""

import pytest
from velune.cognition.council.critic_agent import CriticAgent
from velune.cognition.council.critic_configs import (
    SCALABILITY_CONFIG,
    CriticConfig,
)
from velune.cognition.council.critics import (
    ScalabilityCritic,
)
from velune.models.specializations import CouncilRole
from velune.core.types.model import ModelDescriptor, ModelCapabilityProfile, CapabilityLevel
from velune.providers.base import ModelProvider
from velune.core.types.inference import InferenceRequest, InferenceResponse


class MockCriticProvider(ModelProvider):
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
async def test_critic_agent_behaves_identically_to_subclass():
    model = ModelDescriptor(
        id="mock-critic",
        provider="mock",
        name="Mock Critic",
        context_window=4096,
        is_local=False,
        capabilities=ModelCapabilityProfile(coding=CapabilityLevel.ADVANCED),
        speed_tier="fast",
    )
    
    raw_response = '{"passed": false, "issues": ["High contention"], "score": 0.4, "rationale": "Thread contention issues."}'
    provider = MockCriticProvider(raw_response)
    
    critic_agent = CriticAgent(SCALABILITY_CONFIG, model, provider)
    scalability_critic = ScalabilityCritic(model, provider)
    
    # Assert properties match
    assert critic_agent.role == scalability_critic.role
    assert critic_agent.system_prompt == scalability_critic.system_prompt
    assert critic_agent.config == SCALABILITY_CONFIG
    
    # Assert execution behaves identically
    res_agent = await critic_agent.critique("task", "proposal", "context")
    res_subclass = await scalability_critic.critique("task", "proposal", "context")
    
    assert res_agent.passed == res_subclass.passed
    assert res_agent.issues == res_subclass.issues
    assert res_agent.score == res_subclass.score
    assert res_agent.rationale == res_subclass.rationale
    assert res_agent.parse_error is None
    assert res_subclass.parse_error is None


@pytest.mark.asyncio
async def test_fifth_critic_extension():
    # Adding a 5th critic type requires ONLY: create CriticConfig, create alias class
    FIFTH_CONFIG = CriticConfig(
        name="SecurityRisk",
        council_role=CouncilRole.REVIEWER,
        system_prompt="Analyze risk level.",
        output_fields={"passed": True, "issues": [], "score": 1.0, "rationale": ""},
        temperature=0.2,
    )
    
    class SecurityRiskCritic(CriticAgent):
        def __init__(self, model: ModelDescriptor, provider: ModelProvider) -> None:
            super().__init__(FIFTH_CONFIG, model, provider)

    model = ModelDescriptor(
        id="mock-critic",
        provider="mock",
        name="Mock Critic",
        context_window=4096,
        is_local=False,
        capabilities=ModelCapabilityProfile(coding=CapabilityLevel.ADVANCED),
        speed_tier="fast",
    )
    
    provider = MockCriticProvider('{"passed": true, "issues": [], "score": 0.9, "rationale": "Low risk"}')
    critic = SecurityRiskCritic(model, provider)
    
    assert critic.role == CouncilRole.REVIEWER
    assert critic.system_prompt == "Analyze risk level."
    assert critic.config.temperature == 0.2
    
    res = await critic.critique("task", "proposal", "context")
    assert res.passed is True
    assert res.score == 0.9
    assert res.rationale == "Low risk"
    assert res.parse_error is None


@pytest.mark.asyncio
async def test_critic_agent_unparseable_json():
    model = ModelDescriptor(
        id="mock-critic",
        provider="mock",
        name="Mock Critic",
        context_window=4096,
        is_local=False,
        capabilities=ModelCapabilityProfile(coding=CapabilityLevel.ADVANCED),
        speed_tier="fast",
    )
    
    provider = MockCriticProvider("Invalid raw text response")
    critic_agent = CriticAgent(SCALABILITY_CONFIG, model, provider)
    
    res = await critic_agent.critique("task", "proposal", "context")
    assert res.parse_error is not None
    assert "Critic output was unparseable:" in res.rationale
