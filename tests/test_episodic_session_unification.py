"""Tests for the session-store unification fix.

Root cause (audit finding): `VeluneREPL._session_id` (JSON `SessionStore`'s
id, minted at `repl.py:76`) and `EpisodicMemory.start_session()`'s own
independently-minted `ses-<hex>` id were two unrelated identifiers for the
same live session — `/session list` (REPL) queried the SQLite id,
`velune session list` (CLI) queried the JSON id, and they could diverge.
Fixed by letting `start_session()` accept the JSON store's id and use it
verbatim; `_cmd_session_list` now reads the JSON store directly instead of
the episodic tier.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from velune.memory.storage.sqlite_pool import SQLiteConnectionPool
from velune.memory.tiers.episodic import EpisodicMemory


async def _make_episodic(db_path: Path) -> tuple[EpisodicMemory, SQLiteConnectionPool]:
    pool = SQLiteConnectionPool(db_path)
    await pool.startup()
    episodic = EpisodicMemory(pool)
    await episodic.initialize()
    return episodic, pool


# ── start_session() identity ────────────────────────────────────────────


async def test_start_session_uses_the_given_id_verbatim(tmp_path):
    episodic, pool = await _make_episodic(tmp_path / "cognitive.db")
    try:
        returned = await episodic.start_session(
            workspace_root=str(tmp_path), model="m1", mode="normal", session_id="a1b2c3d4"
        )
        assert returned == "a1b2c3d4"

        sessions = await episodic.list_recent_sessions(str(tmp_path))
        assert [s.id for s in sessions] == ["a1b2c3d4"]
    finally:
        await pool.shutdown()


async def test_start_session_without_an_id_still_automints_as_before(tmp_path):
    """Backward compatibility: the only other caller
    (tests/integration/test_memory_persistence.py) never passes session_id
    and must keep getting today's auto-minted ses-<hex> id."""
    episodic, pool = await _make_episodic(tmp_path / "cognitive.db")
    try:
        returned = await episodic.start_session(
            workspace_root=str(tmp_path), model="m1", mode="normal"
        )
        assert returned.startswith("ses-")
    finally:
        await pool.shutdown()


async def test_turns_recorded_under_the_unified_id_are_retrievable(tmp_path):
    """The whole point: turns, search, and summary all key off the JSON
    store's id once it's threaded through, so they stay correlated."""
    episodic, pool = await _make_episodic(tmp_path / "cognitive.db")
    try:
        session_id = "deadbeef"
        await episodic.start_session(
            workspace_root=str(tmp_path), model="m1", mode="normal", session_id=session_id
        )
        await episodic.record_turn(session_id=session_id, role="user", content="hello")

        history = await episodic.get_session_history(session_id)
        assert len(history) == 1
        assert history[0].content == "hello"

        hits = await episodic.search_by_content("hello", str(tmp_path))
        assert any(h.session_id == session_id for h in hits)
    finally:
        await pool.shutdown()


# ── /session list reads the canonical JSON store ────────────────────────


async def test_cmd_session_list_reads_from_the_json_store_not_episodic(tmp_path, monkeypatch):
    from rich.console import Console

    from velune.cli.handlers.session_mgmt import _cmd_session_list
    from velune.cli.sessions import SessionStore

    monkeypatch.setattr("velune.cli.sessions.DEFAULT_SESSIONS_DIR", tmp_path / "sessions")
    store = SessionStore(root=tmp_path / "sessions")
    store.save(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
        workspace=str(tmp_path),
        model_id="gpt-4o",
        title="Test session",
    )

    repl = MagicMock()
    repl.console = Console(record=True, width=120)
    repl._session_store = store
    # If _cmd_session_list ever falls back to episodic again, this would
    # trip immediately — asserting the container is never touched at all
    # is the actual regression guard.
    repl.container.get.side_effect = AssertionError(
        "must not query the container/episodic tier for session listing"
    )

    await _cmd_session_list(repl, str(tmp_path))

    printed = repl.console.export_text()
    assert "Test session" in printed
    assert "gpt-4o" in printed


async def test_cmd_session_list_handles_an_empty_store(tmp_path, monkeypatch):
    from velune.cli.handlers.session_mgmt import _cmd_session_list
    from velune.cli.sessions import SessionStore

    monkeypatch.setattr("velune.cli.sessions.DEFAULT_SESSIONS_DIR", tmp_path / "sessions")
    store = SessionStore(root=tmp_path / "sessions")

    repl = MagicMock()
    repl.console = MagicMock()
    repl._session_store = store

    await _cmd_session_list(repl, str(tmp_path))

    # No exception, and some "no sessions" notification was printed.
    assert repl.console.print.called
