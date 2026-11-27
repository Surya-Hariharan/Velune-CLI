"""Session ids across /new, /fork, /session save and /mode.

Each test pins a bug that was fixed: duplicate session files from /session save, an old
autosave left behind by /fork (reported as a crash on the next launch), /rewind reaching
into the conversation before /new, and /mode fast changing the model without saving it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from velune.cli.handlers.session import cmd_fork, cmd_new
from velune.cli.handlers.session_mgmt import cmd_session
from velune.cli.modes import MODE_CONFIGS, SessionMode
from velune.cli.sessions import SessionStore

_TALK = [
    {"role": "user", "content": "step one"},
    {"role": "assistant", "content": "did step one"},
]


def _repl(tmp_path, conversation=_TALK):
    repl = MagicMock()
    repl.container.get.return_value = str(tmp_path)
    repl._conversation = list(conversation)
    repl._session_store = SessionStore(root=tmp_path / "sessions")
    repl._session_id = "live-id"
    repl.active_model = MagicMock(model_id="gpt-4o")
    repl._mode_manager.current = SessionMode.NORMAL
    repl.session_tokens = 7
    repl._end_episodic_session = AsyncMock()
    repl._start_episodic_session = AsyncMock()
    return repl


def _autosave(repl):
    repl._session_store.autosave(
        repl._conversation, session_id=repl._session_id, workspace="", model_id="m"
    )
    return repl._session_store.autosave_dir / f"{repl._session_id}.json"


async def test_session_save_twice_keeps_one_file_under_the_session_id(tmp_path):
    repl = _repl(tmp_path)
    await cmd_session(repl, "save")
    repl._conversation.append({"role": "user", "content": "more"})
    await cmd_session(repl, "save")

    saved = repl._session_store.list(limit=10)
    assert [m.id for m in saved] == ["live-id"]
    assert repl._session_store.load("live-id")[1][-1]["content"] == "more"


async def test_fork_archives_under_the_old_id_and_clears_its_autosave(tmp_path):
    repl = _repl(tmp_path)
    sidecar = _autosave(repl)

    await cmd_fork(repl, "")

    assert not sidecar.exists()
    assert repl._session_store.load("live-id")[1] == _TALK
    assert repl._session_id != "live-id"


async def test_new_starts_a_new_id_and_forgets_the_old_turn_journal(tmp_path):
    repl = _repl(tmp_path)
    sidecar = _autosave(repl)
    repl._last_turn_journal = object()

    await cmd_new(repl, "")

    assert repl._session_id != "live-id"
    assert repl._last_turn_journal is None
    assert repl._conversation == []
    assert not sidecar.exists()
    assert repl._session_store.load("live-id")[1] == _TALK


async def test_mode_fast_switches_the_model_through_activate_model(monkeypatch):
    from velune.cli.handlers import mode, model

    chosen = MagicMock(model_id="small")
    activated = []

    async def fake_activate(repl, m):
        activated.append(m)

    monkeypatch.setattr(model, "activate_model", fake_activate)
    monkeypatch.setattr(
        "velune.cli.model_selector.ModeAwareModelSelector.select_for_mode",
        lambda self, config, current: chosen,
    )
    repl = MagicMock()
    repl._mode_manager.set_mode.return_value = MODE_CONFIGS[SessionMode.OPTIMUS]
    repl.active_model = MagicMock(model_id="big")
    await mode.cmd_mode(repl, "fast")
    assert activated == [chosen]


async def test_mode_fast_without_a_model_registry_only_changes_the_mode():
    from velune.cli.handlers import mode

    repl = MagicMock()
    repl._mode_manager.set_mode.return_value = MODE_CONFIGS[SessionMode.OPTIMUS]
    repl.container.get_optional.return_value = None
    repl.active_model = None
    await mode.cmd_mode(repl, "fast")
    repl._mode_manager.set_mode.assert_called_once_with(SessionMode.OPTIMUS)
