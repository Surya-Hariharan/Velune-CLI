"""Integration tests for the Phase 1 memory-lifecycle convergence.

Unlike the mocked unit tests in tests/test_memory_lifecycle.py and
tests/test_three_brain_memory.py, these exercise real, file-backed SQLite
storage (SQLiteConnectionPool + EpisodicMemory + WorkingMemoryTier) wired
through the actual ThreeBrainCoordinator and MemoryLifecycleManager classes —
the same objects velune/memory/subsystems.py constructs for the live REPL —
to verify the convergence claims hold end-to-end, not just against mocks:

- record_turn() durably persists to real SQLite (not just an in-memory list).
- ThreeBrainCoordinator.query() sees a turn recorded moments earlier.
- Turns survive a simulated session restart (new pool/instances, same file).
- Compaction actually triggers and produces a real summary turn once a real
  (fake) inference provider is wired in — regression guard for the bug where
  the provider was hardcoded to None and summarization silently always failed.

Run with: pytest tests/integration/test_memory_persistence.py -v -m integration
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from velune.memory.lifecycle import MemoryLifecycleManager
from velune.memory.storage.sqlite_pool import SQLiteConnectionPool
from velune.memory.three_brain import ThreeBrainCoordinator
from velune.memory.tiers.episodic import EpisodicMemory
from velune.memory.tiers.working import WorkingMemoryTier

pytestmark = pytest.mark.integration


class _FakeProvider:
    """Minimal inference provider stand-in for ContextCompactor's summarizer call."""

    def __init__(self, summary: str) -> None:
        self._summary = summary

    async def infer(self, request):
        return SimpleNamespace(content=self._summary)


async def _make_manager(db_path: Path, provider=None) -> tuple[MemoryLifecycleManager, SQLiteConnectionPool]:
    pool = SQLiteConnectionPool(db_path)
    await pool.startup()
    episodic = EpisodicMemory(pool)
    await episodic.initialize()
    working = WorkingMemoryTier()
    coordinator = ThreeBrainCoordinator(working, None, episodic)

    registry = None
    if provider is not None:
        registry = SimpleNamespace(get=lambda name: provider if name == "ollama" else None)

    manager = MemoryLifecycleManager(
        working_tier=working,
        episodic_memory=episodic,
        semantic_memory=None,
        embedding_pipeline=None,
        lineage_tier=None,
        three_brain=coordinator,
        provider_registry=registry,
    )
    return manager, pool


async def test_fresh_workspace_first_turn_persists_to_real_sqlite(tmp_path):
    manager, pool = await _make_manager(tmp_path / "cognitive.db")
    try:
        session_id = await manager.episodic_memory.start_session(
            workspace_root=str(tmp_path), model="m1", mode="normal"
        )
        turn_id = await manager.record_turn(
            session_id=session_id, role="user", content="hello velune", model="m1"
        )
        assert turn_id  # real UUID-derived id, not empty
        assert len(manager.working.get_turns()) == 1

        turns = await manager.episodic_memory.get_session_history(session_id)
        assert len(turns) == 1
        assert turns[0].content == "hello velune"
    finally:
        await pool.shutdown()


async def test_second_turn_coordinator_query_sees_first_turn_in_episodic(tmp_path):
    manager, pool = await _make_manager(tmp_path / "cognitive.db")
    try:
        session_id = await manager.episodic_memory.start_session(
            workspace_root=str(tmp_path), model="m1", mode="normal"
        )
        await manager.record_turn(session_id=session_id, role="user", content="what is auth flow")
        await manager.record_turn(
            session_id=session_id, role="assistant", content="auth flow uses JWT tokens"
        )

        context = await manager.retrieve("auth flow", workspace_root=session_id, budget=4000)
        contents = [r.content for r in context.results]
        assert any("JWT" in c or "auth flow" in c for c in contents)
    finally:
        await pool.shutdown()


async def test_session_restart_turns_survive_new_pool_instance(tmp_path):
    db_path = tmp_path / "cognitive.db"
    manager1, pool1 = await _make_manager(db_path)
    session_id = await manager1.episodic_memory.start_session(
        workspace_root=str(tmp_path), model="m1", mode="normal"
    )
    await manager1.record_turn(session_id=session_id, role="user", content="remember this")
    await pool1.shutdown()

    # Simulate a full process restart: brand-new pool/episodic/working/manager
    # instances pointed at the same on-disk file.
    manager2, pool2 = await _make_manager(db_path)
    try:
        turns = await manager2.episodic_memory.get_session_history(session_id)
        assert len(turns) == 1
        assert turns[0].content == "remember this"
    finally:
        await pool2.shutdown()


async def test_compaction_triggers_and_produces_real_summary_turn(tmp_path):
    """Regression guard for the fixed compaction-provider bug: with a real
    provider resolved (not the old hardcoded None), ContextCompactor must
    actually run should_compact -> compact -> record a summary turn."""
    padded_content = "turn content padded so token estimates are meaningfully large. " * 3
    summary_text = (
        "- Decision: adopted JWT for authentication\n"
        "- Bug fixed: token expiry off-by-one error\n"
        "- Next: add refresh-token rotation support\n"
    )
    provider = _FakeProvider(summary_text)
    manager, pool = await _make_manager(tmp_path / "cognitive.db", provider=provider)
    try:
        session_id = await manager.episodic_memory.start_session(
            workspace_root=str(tmp_path), model="m1", mode="normal"
        )
        before_tasks = asyncio.all_tasks()
        # record_turn() already calls _check_and_trigger_compaction internally
        # on every turn (see lifecycle.py); the 31st call is what crosses
        # MIN_TURNS_FOR_COMPACTION and schedules the background compaction
        # task once. Triggering it a second time here would race the
        # background task and double-compact — so we only await what this
        # loop itself scheduled.
        for i in range(31):
            await manager.record_turn(
                session_id=session_id, role="user", content=f"{padded_content} #{i}"
            )
        new_tasks = asyncio.all_tasks() - before_tasks
        await asyncio.gather(*new_tasks)

        working_turns = manager.working.get_turns()
        # KEEP_LAST_N_TURNS (10) + 1 summary turn appended by the compactor.
        assert len(working_turns) <= 11
        assert any(t.metadata.get("type") == "compaction_summary" for t in working_turns)

        episodic_turns = await manager.episodic_memory.get_session_history(session_id)
        assert any(t.content == summary_text for t in episodic_turns)
    finally:
        await pool.shutdown()


async def test_compaction_does_not_trigger_with_few_turns(tmp_path):
    provider = _FakeProvider("short summary that should never be generated here")
    manager, pool = await _make_manager(tmp_path / "cognitive.db", provider=provider)
    try:
        session_id = await manager.episodic_memory.start_session(
            workspace_root=str(tmp_path), model="m1", mode="normal"
        )
        await manager.record_turn(session_id=session_id, role="user", content="just one turn")
        await asyncio.sleep(0)  # let any (unexpected) scheduled task run

        working_turns = manager.working.get_turns()
        assert len(working_turns) == 1
        assert working_turns[0].metadata.get("type") != "compaction_summary"
    finally:
        await pool.shutdown()
