"""Extended unit tests for MemoryLifecycleCoordinator and MemoryLifecycleManager."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from velune.memory.lifecycle import (
    MemoryArtifact,
    MemoryLifecycleCoordinator,
    MemoryLifecycleManager,
)


@pytest.mark.asyncio
async def test_coordinator_lifecycle() -> None:
    working = MagicMock()
    episodic = MagicMock()
    episodic.add_turn = AsyncMock()

    turn = MagicMock()
    turn.role = "user"
    turn.content = "test content"
    turn.metadata = {}
    working.get_turns.return_value = [turn]
    working.session_id = "session-123"

    coordinator = MemoryLifecycleCoordinator(working, episodic)
    await coordinator.startup()
    assert coordinator._is_active is True

    await coordinator.shutdown()
    assert coordinator._is_active is False
    episodic.add_turn.assert_called_once_with(
        session_id="session-123",
        role="user",
        content="test content",
        metadata={},
    )


@pytest.mark.asyncio
async def test_coordinator_get_recent_context() -> None:
    episodic = MagicMock()
    turn1 = MagicMock(role="user", content="hello", timestamp=100.0, metadata={})
    turn2 = MagicMock(role="assistant", content="hi", timestamp=101.0, metadata={})

    # Test happy path
    episodic.get_turns = AsyncMock(return_value=[turn1, turn2])
    coordinator = MemoryLifecycleCoordinator(None, episodic)

    recent = await coordinator.get_recent_context("session-123", limit=1)
    assert len(recent) == 1
    assert recent[0]["content"] == "hi"

    # Test episodic None path
    coordinator_none = MemoryLifecycleCoordinator(None, None)
    assert await coordinator_none.get_recent_context("session-123") == []

    # Test error path
    episodic.get_turns.side_effect = Exception("db error")
    assert await coordinator.get_recent_context("session-123") == []


def test_coordinator_ingest() -> None:
    working = MagicMock()
    episodic = MagicMock()
    episodic.add_turn = AsyncMock()

    coordinator = MemoryLifecycleCoordinator(working, episodic)
    artifact = MemoryArtifact(
        id="art-1",
        memory_type="summary",
        content="my summary",
        importance=0.9,
        metadata={"run_id": "session-123"},
    )
    coordinator.ingest(artifact)
    working.add_turn.assert_called_once_with(
        role="system",
        content="my summary",
        metadata={"run_id": "session-123"},
    )


def test_coordinator_summary() -> None:
    working = MagicMock()
    working.get_turns.return_value = [1, 2]
    working.get_execution_logs.return_value = [3]

    coordinator = MemoryLifecycleCoordinator(working, None)
    summary_data = coordinator.summary()
    assert summary_data["working_turns"] == 2
    assert summary_data["working_logs"] == 1


@pytest.mark.asyncio
async def test_manager_record_turn() -> None:
    working = MagicMock()
    episodic = MagicMock()
    episodic.record_turn = AsyncMock(return_value="turn-456")
    semantic = MagicMock()
    lineage = MagicMock()

    manager = MemoryLifecycleManager(
        working_tier=working,
        episodic_memory=episodic,
        semantic_memory=semantic,
        embedding_pipeline=MagicMock(),
        lineage_tier=lineage,
    )

    turn_id = await manager.record_turn(
        session_id="session-123",
        role="user",
        content="hello manager",
        model="gpt-4",
        tokens=10,
        workspace_root="/tmp",
    )

    assert turn_id == "turn-456"
    episodic.record_turn.assert_called_once_with(
        session_id="session-123",
        role="user",
        content="hello manager",
        model="gpt-4",
        tokens=10,
    )
    working.add_turn.assert_called_once_with(
        "user", "hello manager", {"model": "gpt-4", "tokens": 10}
    )
    semantic.index_turn.assert_called_once()


@pytest.mark.asyncio
async def test_manager_retrieve() -> None:
    working = MagicMock()
    turn = MagicMock()
    turn.content = "working hit"
    turn.session_id = "session-123"
    turn.timestamp = time.time()
    working.get_recent_turns.return_value = [turn]

    episodic = MagicMock()
    ep_turn = MagicMock()
    ep_turn.content = "episodic hit"
    ep_turn.session_id = "session-123"
    ep_turn.created_at = time.time()
    episodic.search_by_content = AsyncMock(return_value=[ep_turn])

    semantic = MagicMock()
    sem_turn = MagicMock()
    sem_turn.content = "semantic hit"
    sem_turn.session_id = "session-123"
    sem_turn.age_seconds = 100
    sem_turn.distance = 0.1
    sem_turn.trust_score = 0.9
    sem_turn.attribution = "semantic"
    semantic.search = AsyncMock(return_value=[sem_turn])

    manager = MemoryLifecycleManager(
        working_tier=working,
        episodic_memory=episodic,
        semantic_memory=semantic,
        embedding_pipeline=MagicMock(),
        lineage_tier=MagicMock(),
    )

    context = await manager.retrieve("query", "workspace", budget=4000)
    assert len(context.results) == 3
    # Check that age formatting and attributes work correctly
    assert manager._format_age(120) == "2 minutes ago"
    assert manager._format_age(4000) == "an hour ago"
    assert manager._format_age(8000) == "2 hours ago"
    assert manager._format_age(90000) == "yesterday"
    assert manager._format_age(200000) == "2 days ago"


@pytest.mark.asyncio
async def test_manager_warnings_and_health() -> None:
    working = MagicMock()
    turn = MagicMock()
    working.get_turns.return_value = [turn]

    episodic = MagicMock()
    episodic.list_recent_sessions = AsyncMock(return_value=["session-1"])

    embedding = MagicMock()
    embedding._queue.qsize.return_value = 5

    manager = MemoryLifecycleManager(
        working_tier=working,
        episodic_memory=episodic,
        semantic_memory=MagicMock(),
        embedding_pipeline=embedding,
        lineage_tier=MagicMock(),
    )

    health_metrics = await manager.health()
    assert health_metrics.working_memory_turns == 1
    assert health_metrics.episodic_sessions == 1
    assert health_metrics.embedding_queue_depth == 5
    assert isinstance(health_metrics.to_dict(), dict)

    # Lineage warnings (currently stubbed)
    decisions, failures = await manager.get_lineage_warnings("query")
    assert decisions == []
    assert failures == []

    # get_working_context
    working.get_recent_turns.return_value = [turn]
    assert len(await manager.get_working_context("session-1")) == 1
