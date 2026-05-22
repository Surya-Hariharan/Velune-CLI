import pytest
from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor
from velune.providers.registry import ProviderRegistry
from velune.providers.router import ProviderRouter

@pytest.fixture
def mock_registry():
    return ProviderRegistry(config=None)

def test_router_fallback_preference(mock_registry):
    router = ProviderRouter(mock_registry)
    router.set_fallback_chain(["openai", "ollama"])
    
    # We have two planning-capable models
    # One is remote openai, one is local ollama
    model_openai = ModelDescriptor(
        id="gpt-4",
        provider="openai",
        name="GPT-4",
        context_window=8192,
        is_local=False,
        capabilities=ModelCapabilityProfile(planning=CapabilityLevel.ADVANCED),
        speed_tier="fast"
    )
    
    model_ollama = ModelDescriptor(
        id="llama3",
        provider="ollama",
        name="Llama 3",
        context_window=8192,
        is_local=True,
        capabilities=ModelCapabilityProfile(planning=CapabilityLevel.BASIC),
        speed_tier="medium"
    )
    
    # Simple route matching fallback chain
    chosen = router.route_task(
        task_category="planning",
        models_list=[model_ollama, model_openai],
        min_level=CapabilityLevel.BASIC,
        local_preferred=False
    )
    assert chosen is model_openai  # Because openai is first in fallback chain
    
    # If local is preferred, ollama should be picked
    chosen_local = router.route_task(
        task_category="planning",
        models_list=[model_ollama, model_openai],
        min_level=CapabilityLevel.BASIC,
        local_preferred=True
    )
    assert chosen_local is model_ollama

def test_router_minimum_capability_filter(mock_registry):
    router = ProviderRouter(mock_registry)
    router.set_fallback_chain(["openai", "ollama"])
    
    model_basic = ModelDescriptor(
        id="basic-model",
        provider="openai",
        name="Basic Model",
        context_window=4096,
        is_local=False,
        capabilities=ModelCapabilityProfile(coding=CapabilityLevel.BASIC),
        speed_tier="fast"
    )
    
    model_expert = ModelDescriptor(
        id="expert-model",
        provider="ollama",
        name="Expert Model",
        context_window=4096,
        is_local=True,
        capabilities=ModelCapabilityProfile(coding=CapabilityLevel.EXPERT),
        speed_tier="medium"
    )
    
    # If we request ADVANCED level, basic-model should be filtered out
    chosen = router.route_task(
        task_category="coding",
        models_list=[model_basic, model_expert],
        min_level=CapabilityLevel.ADVANCED,
        local_preferred=False
    )
    assert chosen is model_expert
