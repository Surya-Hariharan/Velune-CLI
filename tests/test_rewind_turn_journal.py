"""``/rewind`` undoes the most recent agent turn, and only that turn (CodeRabbit #1 on PR #26).

The turn journal used to be replaced only when the new turn had changed something, so a turn
that merely read a file left the *previous* turn's journal in place and ``/rewind`` then deleted
the previous turn's files. These tests drive the real turn handlers (``run_tool_chat`` and
``apply_council_edits``) and the real ``/rewind`` command.
"""

from __future__ import annotations

import asyncio
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console

from tests.test_tool_chat import ScriptedProvider, _fake_repl, _model, _text, _tool_turn
from velune.cli.handlers import council as council_handlers
from velune.cli.handlers.council import apply_council_edits
from velune.cli.handlers.git import cmd_rewind
from velune.cli.handlers.tool_chat import TurnProviderError, run_tool_chat
from velune.core.errors.provider import ProviderConnectionError
from velune.core.types.inference import InferenceRequest, ToolCall
from velune.tools.base.registry import ToolRegistry
from velune.tools.base.tool import BaseTool, ToolPermission
from velune.tools.filesystem.write import WriteFile


class _ActionTool(BaseTool):
    """A tool that does real, journaled filesystem work (declared read-only so no prompt)."""

    def __init__(self, name: str, workspace: Path, work) -> None:
        self._name, self._workspace, self._work = name, workspace, work

    def get_name(self) -> str:
        return self._name

    def get_description(self) -> str:
        return self._name

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.FILESYSTEM_READ}

    def get_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return await self._work(self._workspace)


def _registry(ws: Path) -> ToolRegistry:
    async def create(root: Path) -> str:
        return await WriteFile(workspace=root, confirm=False).execute(
            file_path="turn_a.txt", content="made in turn A\n"
        )

    async def peek(root: Path) -> str:
        return (root / "other.txt").read_text()

    async def create_then_fail(root: Path) -> str:
        return await create(root)

    registry = ToolRegistry()
    registry.register(_ActionTool("create_a", ws, create))
    registry.register(_ActionTool("peek", ws, peek))
    registry.register(_ActionTool("create_then_fail", ws, create_then_fail))
    return registry


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "other.txt").write_text("unrelated\n")
    return root


@pytest.fixture
def repl(ws: Path):
    value = _fake_repl(ws, registry=_registry(ws))
    value._last_turn_journal = None
    return value


def _request() -> InferenceRequest:
    return InferenceRequest(model_id="test-model", messages=[{"role": "user", "content": "go"}])


async def _turn(repl, *responses) -> Any:
    return await run_tool_chat(repl, _model(), ScriptedProvider(list(responses)), _request())


def _out(repl) -> str:
    return repl.console.file.getvalue()


async def test_a_read_only_turn_does_not_leave_the_previous_turn_to_be_rewound(repl, ws):
    await _turn(
        repl, _tool_turn(ToolCall(id="c1", name="create_a", arguments={})), _text("made it")
    )
    assert (ws / "turn_a.txt").exists()
    # Turn B only reads. Its journal is empty, and it must replace turn A's.
    await _turn(repl, _tool_turn(ToolCall(id="c2", name="peek", arguments={})), _text("looked"))
    assert not repl._last_turn_journal  # empty, but present: turn A's is gone

    await cmd_rewind(repl, "")
    assert (ws / "turn_a.txt").read_text() == "made in turn A\n"  # untouched
    assert "Nothing to rewind" in _out(repl)
    assert "most recent agent turn" in _out(repl)


async def test_rewind_still_undoes_the_turn_that_made_the_change(repl, ws):
    await _turn(
        repl, _tool_turn(ToolCall(id="c1", name="create_a", arguments={})), _text("made it")
    )
    await cmd_rewind(repl, "")
    assert not (ws / "turn_a.txt").exists()
    assert "Rewound 1 of 1" in _out(repl)
    # ...and a second /rewind has nothing left, it does not reach further back.
    await cmd_rewind(repl, "")
    assert "Nothing to rewind" in _out(repl)


async def test_a_turn_with_no_tool_calls_also_replaces_the_journal(repl, ws):
    await _turn(
        repl, _tool_turn(ToolCall(id="c1", name="create_a", arguments={})), _text("made it")
    )
    await _turn(repl, _text("just talking"))
    await cmd_rewind(repl, "")
    assert (ws / "turn_a.txt").exists()


async def test_a_failed_turn_keeps_what_it_changed_and_nothing_older(repl, ws):
    """Error path: the turn wrote a file and then the provider died. Its own change is rewindable."""
    (ws / "older.txt").write_text("from an older turn\n")
    first = await _turn(repl, _tool_turn(ToolCall(id="c1", name="peek", arguments={})), _text("ok"))
    assert first is not None
    repl._last_turn_journal = _journal_that_created(ws / "older.txt")  # an older turn's changes

    with pytest.raises(TurnProviderError):
        await _turn(
            repl,
            _tool_turn(ToolCall(id="c2", name="create_then_fail", arguments={})),
            ProviderConnectionError("connection dropped"),
        )
    assert (ws / "turn_a.txt").exists()
    await cmd_rewind(repl, "")
    assert not (ws / "turn_a.txt").exists()  # the failed turn's own change was undone
    assert (ws / "older.txt").exists()  # the older turn was not touched


async def test_a_failed_turn_that_changed_nothing_still_replaces_the_journal(repl, ws):
    (ws / "older.txt").write_text("from an older turn\n")
    repl._last_turn_journal = _journal_that_created(ws / "older.txt")
    with pytest.raises(TurnProviderError):
        await _turn(repl, ProviderConnectionError("refused"))
    await cmd_rewind(repl, "")
    assert (ws / "older.txt").exists()


async def test_a_cancelled_turn_keeps_its_own_changes_and_replaces_the_journal(repl, ws):
    (ws / "older.txt").write_text("from an older turn\n")
    repl._last_turn_journal = _journal_that_created(ws / "older.txt")

    class Cancelling(ScriptedProvider):
        async def infer(self, request):
            if self.requests:
                raise asyncio.CancelledError
            return await super().infer(request)

    provider = Cancelling([_tool_turn(ToolCall(id="c1", name="create_a", arguments={}))])
    with pytest.raises(asyncio.CancelledError):
        await run_tool_chat(repl, _model(), provider, _request())
    await cmd_rewind(repl, "")
    assert not (ws / "turn_a.txt").exists()
    assert (ws / "older.txt").exists()


def _journal_that_created(path: Path):
    from velune.tools.filesystem import mutation as mut

    journal = mut.TurnJournal()
    journal.add(mut.FileChange(path, None, mut.file_sha256(path)))
    return journal


# ── Council ────────────────────────────────────────────────────────────────────────────────────

PROPOSAL = "edited.py\n<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n"
NO_MATCH = "edited.py\n<<<<<<< SEARCH\nnot there\n=======\ny\n>>>>>>> REPLACE\n"


@pytest.fixture
def council_repl(ws: Path, monkeypatch):
    (ws / "edited.py").write_text("x = 1\n", encoding="utf-8")

    async def allow(repl, file_writes):
        return True

    monkeypatch.setattr(council_handlers, "_authorize_council_edits", allow)
    container = SimpleNamespace(get=lambda key: ws, get_optional=lambda key, default=None: default)
    value = SimpleNamespace(
        container=container,
        console=Console(file=StringIO(), force_terminal=False, width=120),
        active_model=None,
        _hunk_review_mode=False,
        _last_turn_journal=None,
    )
    (ws / "older.txt").write_text("from an older turn\n")
    value._last_turn_journal = _journal_that_created(ws / "older.txt")
    return value


async def test_a_council_turn_that_writes_replaces_the_journal_with_its_own(council_repl, ws):
    await apply_council_edits(council_repl, PROPOSAL, "bump x")
    assert (ws / "edited.py").read_text() == "x = 2\n"
    await cmd_rewind(council_repl, "")
    assert (ws / "edited.py").read_text() == "x = 1\n"  # the council edit was undone
    assert (ws / "older.txt").exists()  # the older turn was not


@pytest.mark.parametrize(
    "proposal", ["no edit blocks here, just prose", NO_MATCH], ids=["no-blocks", "nothing-matches"]
)
async def test_a_council_turn_that_applies_nothing_must_not_expose_an_older_turn(
    council_repl, ws, proposal
):
    await apply_council_edits(council_repl, proposal, "bump x")
    await cmd_rewind(council_repl, "")
    assert (ws / "older.txt").exists()
    assert "Nothing to rewind" in council_repl.console.file.getvalue()


async def test_a_council_turn_whose_changes_are_all_rejected_must_not_expose_an_older_turn(
    council_repl, ws, monkeypatch
):
    async def refuse(repl, file_writes):
        return None  # PLAN mode / policy refusal

    monkeypatch.setattr(council_handlers, "_authorize_council_edits", refuse)
    await apply_council_edits(council_repl, PROPOSAL, "bump x")
    await cmd_rewind(council_repl, "")
    assert (ws / "older.txt").exists()
    assert (ws / "edited.py").read_text() == "x = 1\n"


# ── Directory moves: say what /rewind actually did (CodeRabbit #8, accepted with this wording) ──


async def test_rewinding_a_folder_move_says_the_contents_were_not_compared(tmp_path):
    """A moved folder has no content hash, so a later change inside it cannot be detected.
    The folder is moved back as a whole, and the report must not claim more than that."""
    from velune.tools.filesystem import mutation as mut
    from velune.tools.filesystem.dirs import MovePath

    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f.txt").write_text("orig\n")
    journal = mut.TurnJournal()
    token = mut.set_active_journal(journal)
    try:
        await MovePath(workspace=tmp_path).execute(source="d", destination="moved")
    finally:
        mut.reset_active_journal(token)
    (tmp_path / "moved" / "added_later.txt").write_text("added by the user\n")

    report = journal.rewind()

    assert (tmp_path / "d" / "f.txt").read_text() == "orig\n"
    assert (tmp_path / "d" / "added_later.txt").exists()  # nothing was lost
    (line,) = report.restored
    assert "folder moved back as a whole" in line and "not compared" in line


async def test_rewinding_a_file_move_is_reported_without_the_caveat(tmp_path):
    from velune.tools.filesystem import mutation as mut
    from velune.tools.filesystem.dirs import MovePath

    (tmp_path / "a.txt").write_text("x\n")
    journal = mut.TurnJournal()
    token = mut.set_active_journal(journal)
    try:
        await MovePath(workspace=tmp_path).execute(source="a.txt", destination="b.txt")
    finally:
        mut.reset_active_journal(token)
    report = journal.rewind()
    assert (tmp_path / "a.txt").exists()
    assert "not compared" not in report.restored[0]
