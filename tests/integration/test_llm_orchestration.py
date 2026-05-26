"""Integration tests for LLM-backed reasoning and planning nodes."""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from velune.orchestration.engine import LangGraphOrchestrationEngine
from velune.orchestration.schemas import OrchestrationState, OrchestrationRequest, ExecutionStatus
from velune.retrieval.schemas import RetrievalResult, RetrievalQuery
from velune.core.types.task import TaskPlan, TaskStep, TaskStatus


@pytest.fixture
def setup_container_with_mock_provider(mock_provider_with_json):
    from velune.kernel.registry import get_container
    container = get_container()
    
    # Save original registrations to restore later
    original = {}
    for key in ["runtime.provider_registry", "runtime.model_registry", "runtime.config"]:
        if container.has(key):
            original[key] = container.get(key)
            
    # Define and register mocks
    class MockProviderRegistry:
        def __init__(self, provider):
            self.provider = provider
        def get(self, name):
            return self.provider
            
    class MockModelEntry:
        def __init__(self, model_id):
            self.model_id = model_id

    class MockModelRegistry:
        def get_by_provider(self, provider_name):
            return [MockModelEntry("test-model-id")]
            
    class MockProvidersConfig:
        default_provider = "openai"

    class MockConfig:
        providers = MockProvidersConfig()
        
    container.register_instance("runtime.provider_registry", MockProviderRegistry(mock_provider_with_json))
    container.register_instance("runtime.model_registry", MockModelRegistry())
    container.register_instance("runtime.config", MockConfig())
    
    yield mock_provider_with_json
    
    # Restore original registrations or clear if they didn't exist
    for key in ["runtime.provider_registry", "runtime.model_registry", "runtime.config"]:
        if key in original:
            container.register_instance(key, original[key])
        else:
            if key in container._singletons:
                del container._singletons[key]


@pytest.mark.asyncio
async def test_reasoning_node_with_mock_provider_llm_mode(monkeypatch, setup_container_with_mock_provider):
    """Set VELUNE_LLM_ORCHESTRATION=true.

    Run _reasoning_node with MockModelProvider returning valid JSON.
    Assert state.execution_state['reasoning'] is populated from LLM response.
    Assert state.execution_state['requires_tools'] is True.
    """
    monkeypatch.setenv("VELUNE_LLM_ORCHESTRATION", "true")
    
    provider = setup_container_with_mock_provider
    provider.response_content = json.dumps({
        "strategy": "Modern AI execution flow with extreme safety validation.",
        "key_files": ["main.py"],
        "risks": ["none"],
        "requires_tools": True,
        "confidence": 0.95
    })
    
    engine = LangGraphOrchestrationEngine(
        retrieval=MagicMock(),
        repository_cognition=MagicMock(),
        memory_lifecycle=MagicMock(),
        graph_memory=MagicMock(),
        tool_registry=MagicMock(),
    )
    
    # Mock checkpoint
    engine._checkpoint = AsyncMock()
    
    request = OrchestrationRequest(prompt="Deploy model", workspace=".")
    state = OrchestrationState(
        run_id="test_run",
        request=request,
        status=ExecutionStatus.IN_PROGRESS,
        task_state={"task_id": "test-task"},
        execution_state={"max_retries": 2, "attempt": 0},
    )
    # Add minimal retrieval results
    query = RetrievalQuery(text="Deploy model")
    state.retrieval_result = RetrievalResult(query=query, hits=[])
    
    state = await engine._reasoning_node(state)
    
    assert provider.call_count == 1
    assert state.execution_state["reasoning"] == "Modern AI execution flow with extreme safety validation."
    assert state.execution_state["requires_tools"] is True
    assert state.execution_state["key_files"] == ["main.py"]
    assert state.execution_state["confidence"] == 0.95


@pytest.mark.asyncio
async def test_reasoning_node_fallback_on_llm_failure(monkeypatch, setup_container_with_mock_provider):
    """Set VELUNE_LLM_ORCHESTRATION=true.

    Use MockModelProvider that raises TimeoutError.
    Run _reasoning_node.
    Assert NO exception raised.
    Assert state still has requires_tools set.
    """
    monkeypatch.setenv("VELUNE_LLM_ORCHESTRATION", "true")
    
    provider = setup_container_with_mock_provider
    
    # Make provider infer raise TimeoutError
    async def raise_timeout(*args, **kwargs):
        raise asyncio.TimeoutError("LLM inference timeout")
    provider.infer = raise_timeout
    
    engine = LangGraphOrchestrationEngine(
        retrieval=MagicMock(),
        repository_cognition=MagicMock(),
        memory_lifecycle=MagicMock(),
        graph_memory=MagicMock(),
        tool_registry=MagicMock(),
    )
    engine._checkpoint = AsyncMock()
    
    request = OrchestrationRequest(prompt="Deploy model", workspace=".")
    state = OrchestrationState(
        run_id="test_run",
        request=request,
        status=ExecutionStatus.IN_PROGRESS,
        task_state={"task_id": "test-task"},
        execution_state={"max_retries": 2, "attempt": 0},
    )
    query = RetrievalQuery(text="Deploy model")
    state.retrieval_result = RetrievalResult(query=query, hits=[])
    
    # Should not raise any exception
    state = await engine._reasoning_node(state)
    
    # Assert it fell back to legacy deterministic path
    assert "Execution strategy for 'Deploy model'" in state.execution_state["reasoning"]
    assert state.execution_state["requires_tools"] is True


@pytest.mark.asyncio
async def test_planning_node_with_llm_produces_valid_plan(monkeypatch, setup_container_with_mock_provider):
    """Set VELUNE_LLM_ORCHESTRATION=true.

    Use MockModelProvider returning valid JSON array of steps.
    Run _planning_node.
    Assert state.task_plan has steps with non-empty descriptions.
    """
    monkeypatch.setenv("VELUNE_LLM_ORCHESTRATION", "true")
    
    provider = setup_container_with_mock_provider
    provider.response_content = json.dumps([
        {"id": "step-1", "description": "Gather project requirements and stage constraints.", "agent_role": "planner", "dependencies": []},
        {"id": "step-2", "description": "Implement changes to main.py.", "agent_role": "coder", "dependencies": ["step-1"]},
        {"id": "step-3", "description": "Review main.py correctness.", "agent_role": "reviewer", "dependencies": ["step-2"]}
    ])
    
    engine = LangGraphOrchestrationEngine(
        retrieval=MagicMock(),
        repository_cognition=MagicMock(),
        memory_lifecycle=MagicMock(),
        graph_memory=MagicMock(),
        tool_registry=MagicMock(),
    )
    engine._checkpoint = AsyncMock()
    
    request = OrchestrationRequest(prompt="Create python model", workspace=".")
    state = OrchestrationState(
        run_id="test_run",
        request=request,
        status=ExecutionStatus.IN_PROGRESS,
        task_state={"task_id": "test-task"},
        execution_state={"max_retries": 2, "attempt": 0},
    )
    
    state = await engine._planning_node(state)
    
    assert provider.call_count == 1
    assert state.task_plan is not None
    assert len(state.task_plan.steps) == 3
    assert state.task_plan.steps[0].description == "Gather project requirements and stage constraints."
    assert state.task_plan.steps[0].agent_role == "planner"


@pytest.mark.asyncio
async def test_deterministic_mode_unchanged(monkeypatch, setup_container_with_mock_provider):
    """With VELUNE_LLM_ORCHESTRATION=false (default).

    Run _reasoning_node.
    Assert MockModelProvider.call_count == 0 (no LLM calls).
    """
    monkeypatch.setenv("VELUNE_LLM_ORCHESTRATION", "false")
    
    provider = setup_container_with_mock_provider
    
    engine = LangGraphOrchestrationEngine(
        retrieval=MagicMock(),
        repository_cognition=MagicMock(),
        memory_lifecycle=MagicMock(),
        graph_memory=MagicMock(),
        tool_registry=MagicMock(),
    )
    engine._checkpoint = AsyncMock()
    
    request = OrchestrationRequest(prompt="Deploy model", workspace=".")
    state = OrchestrationState(
        run_id="test_run",
        request=request,
        status=ExecutionStatus.IN_PROGRESS,
        task_state={"task_id": "test-task"},
        execution_state={"max_retries": 2, "attempt": 0},
    )
    query = RetrievalQuery(text="Deploy model")
    state.retrieval_result = RetrievalResult(query=query, hits=[])
    
    state = await engine._reasoning_node(state)
    
    assert provider.call_count == 0
    assert "Execution strategy for 'Deploy model'" in state.execution_state["reasoning"]
