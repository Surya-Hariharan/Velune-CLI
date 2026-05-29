import pytest
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock
from velune.cognition.council.factory import CouncilAgentFactory
from velune.cognition.style_resolver import StyleResolver
from velune.cognition.council.tiers import TierClassifier, CouncilTier
from velune.models.specializations import CouncilRole
from velune.cognition.orchestrator import CouncilOrchestrator

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
        self.map_calls = 0
        
    def map_roles(self, *args, **kwargs):
        self.map_calls += 1
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
    def __init__(self):
        pass
    async def infer(self, request):
        mock_response = MagicMock()
        mock_response.content = "Mock response implementation code"
        return mock_response
    def get_capabilities(self):
        capabilities = MagicMock()
        capabilities.supports_streaming = False
        return capabilities

def build_test_orchestrator():
    provider = MockProvider()
    registry = MockProviderRegistry(provider)
    mapper = MockModelSpecializationMapper()
    
    orchestrator = CouncilOrchestrator(
        provider_registry=registry,
        mapper=mapper,
        lineage_db_path=None,
    )
    orchestrator.lineage_memory = MagicMock()
    orchestrator.lineage_memory.get_personality_style.return_value = None
    orchestrator.lineage_memory.query_continuity_warnings.return_value = ([], [])
    
    return orchestrator

@pytest.mark.asyncio
async def test_agent_factory_construction():
    """Verify that CouncilAgentFactory constructs agents and caches mappings per run."""
    provider = MockProvider()
    registry = MockProviderRegistry(provider)
    mapper = MockModelSpecializationMapper()
    
    factory = CouncilAgentFactory(provider_registry=registry, mapper=mapper)
    
    run_id = "run-test-123"
    
    planner = factory.create_planner(run_id)
    assert planner is not None
    assert planner.model.model_id == "test-model-id"
    assert planner.role == CouncilRole.PLANNER
    
    coder = factory.create_coder(run_id)
    assert coder is not None
    assert coder.role == CouncilRole.CODER

    # Check mapping cache reuse
    assert mapper.map_calls == 1

    # Call with a different run ID
    other_coder = factory.create_coder("run-test-456")
    assert other_coder is not None
    assert mapper.map_calls == 2

@pytest.mark.asyncio
async def test_tier_classifier_policy():
    """Verify that TierClassifier evaluates heuristics and fallback rules correctly."""
    mock_registry = MagicMock()
    mock_registry.pending_count.return_value = 5
    
    classifier = TierClassifier(
        task_registry=mock_registry,
        max_council_tier="full",
        low_resource_mode=False
    )
    
    assert classifier.get_queue_depth() == 5
    
    # Under high queue depth, full should downgrade to standard
    tier = classifier.classify("implement heavy architecture redesign", "context")
    assert tier == CouncilTier.STANDARD

@pytest.mark.asyncio
async def test_style_resolver_async_safety():
    """Verify that StyleResolver runs scans asynchronously and caches results."""
    memory = MagicMock()
    memory.get_personality_style.return_value = None
    
    resolver = StyleResolver(lineage_memory=memory)
    
    # We mock _scan_directory to simulate heavy scanning in a non-blocking way
    resolver._scan_directory = MagicMock(return_value={
        "naming_conventions": {"dominant": "snake_case", "breakdown": {}},
        "type_hinting_strictness": 0.8,
        "preferred_constructs": [],
        "class_vs_functional": "OOP",
        "docstring_style": "Google",
    })
    
    start = time.time()
    profile = await resolver.get_or_refresh_style_profile("velune/core/main.py")
    elapsed = time.time() - start
    
    assert profile is not None
    assert profile["naming_conventions"]["dominant"] == "snake_case"
    assert elapsed < 0.5  # Should run instantly/asynchronously

@pytest.mark.asyncio
async def test_orchestrator_refactored_execution():
    """Verify that the refactored orchestrator runs execution paths successfully."""
    orchestrator = build_test_orchestrator()
    
    result = await orchestrator.execute_task(
        prompt="explain authentication logic",
        repo_context="some context",
        council_tier="instant"
    )
    
    assert result["tier"] == "instant"
    assert "Mock response" in result["final_summary"]
