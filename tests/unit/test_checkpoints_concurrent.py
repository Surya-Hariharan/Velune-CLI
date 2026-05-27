import pytest
import asyncio
from pathlib import Path
from velune.orchestration.checkpoints import SQLiteCheckpointStore
from velune.orchestration.schemas import OrchestrationRequest, OrchestrationState, ExecutionStatus


def build_mock_orchestration_state() -> OrchestrationState:
    request = OrchestrationRequest(prompt="fix auth", workspace="/tmp")
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


@pytest.mark.asyncio
async def test_concurrent_saves_no_collision(tmp_path):
    """Concurrent checkpoint saves must produce distinct IDs."""
    from velune.memory.storage.sqlite_manager import SQLiteManager
    manager = SQLiteManager(tmp_path / "test.db")
    store = SQLiteCheckpointStore(sqlite_manager=manager)
    
    state = build_mock_orchestration_state()
    
    # 10 concurrent saves for same run_id
    tasks = [
        asyncio.create_task(asyncio.to_thread(store.save, "run-1", "planning", state))
        for _ in range(10)
    ]
    checkpoint_ids = await asyncio.gather(*tasks)
    
    # All IDs must be unique
    assert len(set(checkpoint_ids)) == 10
    
    # All must be retrievable
    listed = store.list_ids("run-1")
    assert len(listed) == 10
