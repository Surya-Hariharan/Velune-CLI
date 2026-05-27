# tests/test_orchestration_index.py

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from velune.orchestration.schemas import OrchestrationRequest, OrchestrationState, ExecutionStatus
from velune.orchestration.engine import LangGraphOrchestrationEngine

def build_minimal_engine(repository_cognition):
    mock_bus = MagicMock()
    mock_bus.emit = AsyncMock()
    return LangGraphOrchestrationEngine(
        retrieval=MagicMock(),
        repository_cognition=repository_cognition,
        memory_lifecycle=MagicMock(),
        graph_memory=MagicMock(),
        tool_registry=MagicMock(),
        event_bus=mock_bus,
        workspace_path=None
    )

def build_initial_state(request):
    return OrchestrationState(
        run_id="run-test",
        request=request,
        status=ExecutionStatus.IN_PROGRESS,
        task_state={"task_id": "test-task"},
        execution_state={"max_retries": request.max_retries, "attempt": 0},
        retrieval_state={},
        memory_state={},
        repository_state={},
        context_state={},
    )

def build_mock_snapshot():
    mock_snapshot = MagicMock()
    mock_snapshot.summary = {"summary_key": "val"}
    mock_snapshot.files = []
    mock_snapshot.symbols = []
    return mock_snapshot

@pytest.mark.asyncio  
async def test_index_uses_cache_by_default():
    """index() must be called with force=False by default."""
    mock_cognition = MagicMock()
    mock_snapshot = build_mock_snapshot()
    mock_cognition.index.return_value = mock_snapshot
    
    # Setup minimal engine
    engine = build_minimal_engine(repository_cognition=mock_cognition)
    
    request = OrchestrationRequest(prompt="fix auth", workspace="/tmp")
    state = build_initial_state(request)
    
    await engine._context_reconstruction_node(state)
    
    # Verify index was called with force=False
    mock_cognition.index.assert_called_once_with(force=False)

@pytest.mark.asyncio
async def test_index_force_reindex_from_metadata():
    """force_reindex metadata flag must trigger force=True."""
    mock_cognition = MagicMock()
    mock_cognition.index.return_value = build_mock_snapshot()
    
    engine = build_minimal_engine(repository_cognition=mock_cognition)
    request = OrchestrationRequest(
        prompt="fix auth",
        workspace="/tmp",
        metadata={"force_reindex": True}
    )
    state = build_initial_state(request)
    
    await engine._context_reconstruction_node(state)
    mock_cognition.index.assert_called_once_with(force=True)
