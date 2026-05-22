import pytest
from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor
from velune.models.registry import ModelCapabilityRegistry
from velune.models.scorer import ModelScorer
from velune.models.profiler import ModelProfiler, ModelProfile
from velune.models.specializations import ModelSpecializationMapper, CouncilRole

@pytest.fixture
def mock_registry():
    # Simple registry with mock scanner
    reg = ModelCapabilityRegistry(scanner=None)
    
    # Register a few different models with distinct profiles
    model_a = ModelDescriptor(
        id="claude-3-opus",
        provider="anthropic",
        name="Claude 3 Opus",
        context_window=200000,
        is_local=False,
        capabilities=ModelCapabilityProfile(
            coding=CapabilityLevel.EXPERT,
            reasoning=CapabilityLevel.EXPERT,
            planning=CapabilityLevel.ADVANCED,
            summarization=CapabilityLevel.ADVANCED
        ),
        speed_tier="slow",
        cost_per_1k_tokens=0.015
    )
    
    model_b = ModelDescriptor(
        id="llama-3-8b-instruct",
        provider="ollama",
        name="Llama 3 8B",
        context_window=8192,
        is_local=True,
        capabilities=ModelCapabilityProfile(
            coding=CapabilityLevel.BASIC,
            reasoning=CapabilityLevel.INTERMEDIATE,
            planning=CapabilityLevel.BASIC,
            summarization=CapabilityLevel.INTERMEDIATE
        ),
        speed_tier="fast"
    )
    
    model_c = ModelDescriptor(
        id="qwen-coder-7b",
        provider="ollama",
        name="Qwen 2.5 Coder 7B",
        context_window=16384,
        is_local=True,
        capabilities=ModelCapabilityProfile(
            coding=CapabilityLevel.ADVANCED,
            reasoning=CapabilityLevel.INTERMEDIATE,
            planning=CapabilityLevel.BASIC,
            summarization=CapabilityLevel.BASIC
        ),
        speed_tier="fast"
    )
    
    reg.register(model_a)
    reg.register(model_b)
    reg.register(model_c)
    return reg

def test_model_scorer():
    scorer = ModelScorer()
    
    model_opus = ModelDescriptor(
        id="claude-3-opus",
        provider="anthropic",
        name="Claude 3 Opus",
        context_window=200000,
        is_local=False,
        capabilities=ModelCapabilityProfile(
            coding=CapabilityLevel.EXPERT,
            reasoning=CapabilityLevel.EXPERT
        ),
        speed_tier="slow",
        cost_per_1k_tokens=0.015
    )
    
    # Expert code capability (1.0)
    score = scorer.score(
        model=model_opus,
        task_category="coding",
        required_tokens=4096,
        latency_requirement="medium"
    )
    
    assert score > 0.5  # Opus is strong, despite cost and slow speed

def test_model_profiler():
    profiler = ModelProfiler()
    
    # Record executions
    profiler.record_execution("ollama", "llama-3-8b-instruct", 120.0)
    profiler.record_execution("ollama", "llama-3-8b-instruct", 130.0)
    profiler.record_execution("ollama", "llama-3-8b-instruct", 140.0)
    
    profile = profiler.get_profile("ollama", "llama-3-8b-instruct")
    assert profile is not None
    assert profile.sample_count == 3
    assert abs(profile.avg_latency_ms - 130.0) < 0.1

def test_specialization_mapper(mock_registry):
    scorer = ModelScorer()
    profiler = ModelProfiler()
    
    mapper = ModelSpecializationMapper(
        registry=mock_registry,
        scorer=scorer,
        profiler=profiler
    )
    
    # Map roles
    mappings = mapper.map_roles(task_category="coding", required_tokens=4096)
    
    # Reviewer needs highest reasoning, typically maps to Opus
    assert mappings[CouncilRole.REVIEWER].model_id == "claude-3-opus"
    
    # Coder needs high coding, qwen-coder-7b is better than llama-3-8b-instruct
    assert mappings[CouncilRole.CODER].model_id == "qwen-coder-7b"
    
    # Apply overrides
    mapper.overrides[CouncilRole.PLANNER] = "llama-3-8b-instruct"
    overridden_mappings = mapper.map_roles()
    assert overridden_mappings[CouncilRole.PLANNER].model_id == "llama-3-8b-instruct"
