"""Tests for /fork — start a new session seeded from a slice of the current one.

The audit's stated gap: `/new` always starts empty; there's no way to branch
at an earlier turn and explore a different path without losing the original
thread. `/fork` is deliberately just "a new ordinary session pre-populated
with a prefix" — no session-schema/tree changes, mirroring `/new`'s own
archive-then-reset pattern (`velune/cli/handlers/session.py::cmd_new`).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from velune.cli.handlers.session import cmd_fork
from velune.cli.modes import SessionMode
from velune.cli.sessions import SessionStore


def _make_repl(tmp_path, conversation):
    repl = MagicMock()
    repl.console = MagicMock()
    repl.container.get.return_value = str(tmp_path)
    repl._conversation = list(conversation)
    repl._session_store = SessionStore(root=tmp_path / "sessions")
    repl._session_id = "original-id"
    repl.active_model = MagicMock(model_id="gpt-4o")
    repl._mode_manager.current = SessionMode.NORMAL
    repl.session_tokens = 42
    repl.session_cost = 0.01
    repl._context_tracker = MagicMock()
    repl._end_episodic_session = AsyncMock()
    repl._start_episodic_session = AsyncMock()
    return repl


_FULL = [
    {"role": "user", "content": "step one"},
    {"role": "assistant", "content": "did step one"},
    {"role": "user", "content": "step two"},
    {"role": "assistant", "content": "did step two"},
]


async def test_fork_with_an_index_truncates_the_live_conversation(tmp_path):
    repl = _make_repl(tmp_path, _FULL)

    await cmd_fork(repl, "2")

    assert repl._conversation == _FULL[:2]


async def test_fork_with_no_arg_carries_over_the_full_conversation(tmp_path):
    repl = _make_repl(tmp_path, _FULL)

    await cmd_fork(repl, "")

    assert repl._conversation == _FULL


async def test_fork_preserves_the_original_session_intact(tmp_path):
    repl = _make_repl(tmp_path, _FULL)

    await cmd_fork(repl, "2")

    # The original, full conversation was archived under its own new id —
    # not mutated, not truncated in storage.
    saved = repl._session_store.list(limit=10)
    assert len(saved) == 1
    _meta, conv = repl._session_store.load(saved[0].id)
    assert conv == _FULL


async def test_fork_assigns_a_new_session_id_not_a_resume(tmp_path):
    repl = _make_repl(tmp_path, _FULL)
    original_id = repl._session_id

    await cmd_fork(repl, "2")

    assert repl._session_id != original_id


async def test_fork_out_of_range_index_clamps_instead_of_raising(tmp_path):
    repl = _make_repl(tmp_path, _FULL)

    await cmd_fork(repl, "999")
    assert repl._conversation == _FULL

    repl2 = _make_repl(tmp_path, _FULL)
    await cmd_fork(repl2, "-5")
    assert repl2._conversation == []


async def test_fork_an_empty_conversation_is_a_noop(tmp_path):
    repl = _make_repl(tmp_path, [])

    await cmd_fork(repl, "")

    repl._end_episodic_session.assert_not_awaited()
    repl._start_episodic_session.assert_not_awaited()


async def test_fork_resets_token_and_cost_tracking(tmp_path):
    repl = _make_repl(tmp_path, _FULL)

    await cmd_fork(repl, "2")

    assert repl.session_tokens == 0
    assert repl.session_cost == 0.0


async def test_fork_with_a_non_numeric_index_reports_an_error_and_does_nothing(tmp_path):
    repl = _make_repl(tmp_path, _FULL)

    await cmd_fork(repl, "not-a-number")

    assert repl._conversation == _FULL  # unchanged
    repl._end_episodic_session.assert_not_awaited()
