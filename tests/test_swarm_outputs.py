"""S5: what each output type really delivers (files, build-project, diagram)."""

from __future__ import annotations

import pytest

from tests.swarm_fakes import (
    ReadTool,
    RecordingGate,
    ShellTool,
    calls,
    fake_repl,
    registry,
    system_of,
    text,
)
from tests.test_swarm_run import CODING, LIBRARY, _agent, _answer, _route, _swarm
from velune.permissions import ExecutionMode
from velune.swarm.agent import CHECK_FILES_CONTRACT, OUTPUT_CONTRACTS, _touched
from velune.tools.base.tool import ToolCallContext
from velune.tools.filesystem.write import WriteFile

BUILD = {**CODING, "permission": "edit-workspace", "output": "files"}


def _once(call, then: str):
    """A reply function: make *call* the first time, answer *then* afterwards."""
    state = {"done": False}

    def reply():
        if state["done"]:
            return text(then)
        state["done"] = True
        return calls(call)

    return reply


def _script(plan, **actions):
    def script(request):
        who = _agent(request)
        if who in actions:
            return actions[who]()
        return _answer(plan)(request)

    return script


def _workspace(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    ctx = ToolCallContext(
        run_id="r", actor="swarm", workspace=workspace, gate=RecordingGate(workspace)
    )
    tools = registry(ReadTool(), WriteFile(workspace=workspace, confirm=False))
    return workspace, ctx, tools


def _system_for(provider, agent_id: str) -> str:
    return system_of(next(r for r in provider.requests if _agent(r) == agent_id))


def _tools_for(provider, agent_id: str) -> set[str]:
    request = next(r for r in provider.requests if _agent(r) == agent_id)
    return {t["function"]["name"] for t in request.tools or []}


# ── files ────────────────────────────────────────────────────────────────────


async def test_changed_files_come_from_the_tool_calls_that_worked(tmp_path):
    workspace, ctx, tools = _workspace(tmp_path)
    write = ("write_file", {"file_path": "dna.py", "content": "x = 1\n"})
    script = _script(BUILD, implementer=_once(write, "I also wrote imaginary.py"))
    outcome, provider = await _swarm(tmp_path, script, tools=tools, ctx=ctx)

    assert outcome.files_changed == ["dna.py"]
    assert outcome.warnings == []
    assert CHECK_FILES_CONTRACT in _system_for(provider, "reviewer")
    assert OUTPUT_CONTRACTS["files"] not in _system_for(provider, "reviewer")
    outcome.journal.rewind()
    assert not (workspace / "dna.py").exists()


async def test_a_files_run_that_wrote_nothing_says_so(tmp_path):
    _, ctx, tools = _workspace(tmp_path)
    outcome, _ = await _swarm(tmp_path, _answer(BUILD), tools=tools, ctx=ctx)
    assert outcome.ok and outcome.files_changed == []
    assert outcome.warnings == ["No files were changed."]


def test_failed_calls_do_not_count_and_a_move_counts_both_paths():
    from types import SimpleNamespace

    def invocation(name, arguments, error=False):
        call = SimpleNamespace(name=name, arguments=arguments)
        return SimpleNamespace(call=call, error=error)

    found = _touched(
        [
            invocation("write_file", {"file_path": "a.py"}, error=True),
            invocation("read_file", {"file_path": "b.py"}),
            invocation("move_path", {"source": "old.py", "destination": "pkg\\new.py"}),
            invocation("edit_file", {"file_path": "old.py"}),
        ]
    )
    assert found == ["old.py", "pkg/new.py"]


async def test_a_writing_profile_never_returns_a_diff_to_apply_again():
    import json

    plan, _ = await _route(json.dumps({**CODING, "permission": "edit-workspace"}))
    assert plan.output == "files"


async def test_a_skill_with_no_writers_lets_the_lead_write():
    import json

    answer = {"skill": "planning", "permission": "build-project", "output": "files"}
    plan, _ = await _route(json.dumps(answer))
    assert [a.id for a in plan.agents if a.can_write] == [plan.lead]
    assert any("so the lead" in note for note in plan.notes)


async def test_plan_mode_proposes_instead_of_changing_files(tmp_path):
    workspace, ctx, tools = _workspace(tmp_path)
    outcome, provider = await _swarm(
        tmp_path, _answer(BUILD), tools=tools, ctx=ctx, mode=ExecutionMode.PLAN
    )
    assert outcome.plan.output == "diff" and outcome.warnings == []
    assert any("proposed instead" in note for note in outcome.plan.notes)
    assert "write_file" not in _tools_for(provider, "implementer")
    assert "Plan mode: nothing is changed" in _system_for(provider, "implementer")
    assert "other agents change files" not in _system_for(provider, "implementer")
    assert list(workspace.iterdir()) == []


# ── build-project ────────────────────────────────────────────────────────────


async def test_only_writers_may_run_commands(tmp_path):
    workspace, ctx, _ = _workspace(tmp_path)
    tools = registry(ReadTool(), ShellTool())
    run = ("execute_command", {"command": "pytest"})
    script = _script(
        {**BUILD, "permission": "build-project"},
        tester=_once(run, "tests ran"),
        reviewer=_once(run, "reviewed"),
    )
    outcome, provider = await _swarm(tmp_path, script, tools=tools, ctx=ctx)

    assert "execute_command" in _tools_for(provider, "tester")
    assert "execute_command" not in _tools_for(provider, "reviewer")
    assert ctx.gate.checked == ["execute_command"]  # the tester's call only
    assert outcome.ok


# ── diagram ──────────────────────────────────────────────────────────────────


DIAGRAM = {"skill": "research", "permission": "answer-only", "output": "diagram"}


async def test_the_diagram_is_saved_in_the_run_folder(tmp_path):
    answer = "Here:\n```mermaid\nflowchart LR\n  a --> b\n```\nDone."
    outcome, _ = await _swarm(tmp_path, _answer(DIAGRAM, writer=answer))
    assert outcome.diagram_path == outcome.folder / "diagram.mmd"
    assert outcome.diagram_path.read_text(encoding="utf-8") == "flowchart LR\n  a --> b\n"
    assert outcome.warnings == []


@pytest.mark.parametrize("answer", ["no picture", "```mermaid\n\n```"])
async def test_a_missing_diagram_is_reported(tmp_path, answer):
    outcome, _ = await _swarm(tmp_path, _answer(DIAGRAM, writer=answer))
    assert outcome.diagram_path is None
    assert "none was saved" in outcome.warnings[0]


# ── what the user is shown ───────────────────────────────────────────────────


async def test_council_lists_changed_files_and_how_to_undo(tmp_path, monkeypatch):
    from velune.cli.handlers import council
    from velune.swarm import setup

    async def no_summary(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(setup, "repository_summary", no_summary)
    monkeypatch.setattr(setup, "runs_root", lambda _workspace: tmp_path / "runs")
    monkeypatch.setattr(setup, "load_library", lambda *_a, **_k: LIBRARY)

    workspace, _, tools = _workspace(tmp_path)
    write = ("write_file", {"file_path": "dna.py", "content": "x = 1\n"})
    from tests.swarm_fakes import ScriptedProvider

    provider = ScriptedProvider(_script(BUILD, implementer=_once(write, "wrote it")))
    repl = fake_repl(workspace, provider, tools=tools, mode=ExecutionMode.AUTO)
    await council.execute_council_task(repl, "add gc_content", permission="edit-workspace")

    shown = repl.output.getvalue()
    assert "Changed 1 file(s): dna.py" in shown
    assert "/rewind undoes this run's file changes" in shown
