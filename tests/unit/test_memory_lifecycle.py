"""Regression tests for MemoryLifecycleManager.

Validates that shutdown() correctly flushes working memory turns into
episodic SQLite so turn history survives process exit.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from velune.memory.lifecycle import MemoryLifecycleManager
from velune.memory.tiers.working import WorkingMemoryTier

# ---------------------------------------------------------------------------
# Minimal stub for EpisodicMemoryTier that uses an in-memory list
# ---------------------------------------------------------------------------


class _InMemoryEpisodic:
    """Minimal episodic stub that stores turns in a list for test isolation."""

    def __init__(self) -> None:
        self._turns: list[dict[str, Any]] = []

    async def record_turn(
        self,
        session_id: str,
        role: str,
        content: str,
        model: str | None = None,
        tokens: int | None = None,
    ) -> str:
        turn_id = str(len(self._turns))
        self._turns.append(
            {
                "id": turn_id,
                "session_id": session_id,
                "role": role,
                "content": content,
                "model": model,
                "tokens": tokens,
            }
        )
        return turn_id

    async def get_all_turns(self, session_id: str) -> list[dict[str, Any]]:
        return [t for t in self._turns if t["session_id"] == session_id]

    async def list_recent_sessions(self, workspace_root: str, limit: int = 1000) -> list[Any]:
        seen: dict[str, bool] = {}
        for t in self._turns:
            seen[t["session_id"]] = True
        return list(seen.keys())

    async def search_by_content(
        self, query: str, workspace_root: str, limit: int = 10
    ) -> list[Any]:
        return []


# ---------------------------------------------------------------------------
# Helper to build a MemoryLifecycleManager with the minimal stubs
# ---------------------------------------------------------------------------


def _build_manager(
    session_id: str = "test-session",
) -> tuple[MemoryLifecycleManager, WorkingMemoryTier, _InMemoryEpisodic]:
    working = WorkingMemoryTier(session_id=session_id)
    episodic = _InMemoryEpisodic()

    manager = MemoryLifecycleManager(
        working_tier=working,
        episodic_memory=episodic,
        semantic_memory=None,
        embedding_pipeline=None,
        lineage_tier=None,
    )
    return manager, working, episodic


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestShutdownFlushesWorkingToEpisodic:
    """Verify that shutdown() persists all live working memory turns to episodic."""

    @pytest.mark.asyncio
    async def test_shutdown_flushes_n_turns(self) -> None:
        """Populate N turns -> call shutdown() -> assert all N appear in episodic."""
        n_turns = 7
        session_id = "flush-test-session"
        manager, working, episodic = _build_manager(session_id)

        # Populate N turns directly in working memory
        for i in range(n_turns):
            working.add_turn(
                role="user" if i % 2 == 0 else "assistant",
                content=f"Turn {i}",
            )

        # Pre-condition: nothing in episodic yet
        pre_turns = await episodic.get_all_turns(session_id)
        assert len(pre_turns) == 0, "Episodic should be empty before shutdown"

        # Run shutdown -- this should flush all N turns
        await manager.startup()
        await manager.shutdown()

        # Post-condition: all N turns are now in episodic
        post_turns = await episodic.get_all_turns(session_id)
        assert len(post_turns) == n_turns, (
            f"Expected {n_turns} turns in episodic after shutdown, got {len(post_turns)}"
        )

    @pytest.mark.asyncio
    async def test_shutdown_preserves_content_and_roles(self) -> None:
        """Turn content and roles must be faithfully preserved after flush."""
        session_id = "content-preserve-session"
        manager, working, episodic = _build_manager(session_id)

        expected = [
            ("user", "Hello, what is async?"),
            ("assistant", "Async allows non-blocking I/O."),
            ("user", "Show me an example."),
        ]

        for role, content in expected:
            working.add_turn(role=role, content=content)

        await manager.startup()
        await manager.shutdown()

        stored = await episodic.get_all_turns(session_id)
        assert len(stored) == len(expected)
        for i, (role, content) in enumerate(expected):
            assert stored[i]["role"] == role
            assert stored[i]["content"] == content

    @pytest.mark.asyncio
    async def test_shutdown_skips_evicted_turns(self) -> None:
        """Turns evicted by TTL before shutdown must NOT be flushed to episodic."""
        session_id = "eviction-test-session"
        # Use a very short TTL so we can forcibly expire turns
        working = WorkingMemoryTier(session_id=session_id, ttl_seconds=0.001)
        episodic = _InMemoryEpisodic()

        manager = MemoryLifecycleManager(
            working_tier=working,
            episodic_memory=episodic,
            semantic_memory=None,
            embedding_pipeline=None,
            lineage_tier=None,
        )

        # Add some turns then let them expire
        working.add_turn(role="user", content="I will expire")
        await asyncio.sleep(0.01)  # Ensure TTL is breached

        await manager.startup()
        await manager.shutdown()

        # No turns should have been flushed -- they were all expired
        flushed = await episodic.get_all_turns(session_id)
        assert len(flushed) == 0, "Expired turns should not be flushed to episodic"

    @pytest.mark.asyncio
    async def test_shutdown_noop_when_no_tiers(self) -> None:
        """shutdown() must not raise when working or episodic tier is None."""
        manager = MemoryLifecycleManager(
            working_tier=None,
            episodic_memory=None,
            semantic_memory=None,
            embedding_pipeline=None,
            lineage_tier=None,
        )
        # Must not raise
        await manager.startup()
        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_is_active_lifecycle(self) -> None:
        """_is_active must be False before startup and after shutdown."""
        manager, _, _ = _build_manager()

        assert manager._is_active is False
        await manager.startup()
        assert manager._is_active is True
        await manager.shutdown()
        assert manager._is_active is False

    @pytest.mark.asyncio
    async def test_shutdown_with_zero_turns(self) -> None:
        """shutdown() on a manager with empty working memory must not raise."""
        manager, _, episodic = _build_manager()
        await manager.startup()
        await manager.shutdown()

        # Nothing to flush -- episodic stays empty
        stored = await episodic.get_all_turns("test-session")
        assert len(stored) == 0
