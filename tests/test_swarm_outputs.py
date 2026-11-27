"""S5: what each output type really delivers (files, build-project, diagram)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.integration_fakes import FakeApp, OtherApp, make_env
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
from velune.permissions.gate import Approval
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


# ── integrations ─────────────────────────────────────────────────────────────

INTEGRATIONS = {"skill": "research", "permission": "integrations", "output": "text"}
ENABLE = ("integrations_enable", {"integration": "fakeapp"})


def _integrations(tmp_path, *, max_tools=24, connectors=None):
    """A real integration source per agent over a fake app, and the shared manager."""
    from velune.resources.tools import IntegrationToolSource

    env = make_env(tmp_path, ask=_allow, connectors=connectors)

    def for_task(task):
        source = IntegrationToolSource(env.manager, max_tools=max_tools)
        source.begin_run(task)
        return source

    return env, for_task


def _context(env):
    return ToolCallContext(
        run_id="r", actor="swarm", workspace=env.gate.boundary.workspace_root, gate=env.gate
    )


async def _allow(_tool, _decision):
    return Approval.ALLOW_ONCE


def _steps(*items):
    """A reply function that plays *items* in order, then repeats the last."""
    left = list(items)

    def reply():
        item = left.pop(0) if len(left) > 1 else left[0]
        return text(item) if isinstance(item, str) else calls(item)

    return reply


async def test_every_agent_reads_integrations_and_only_the_lead_changes_them(tmp_path):
    env, factory = _integrations(tmp_path)
    script = _script(
        INTEGRATIONS,
        researcher=_steps(ENABLE, ("fakeapp__poke", {"key": "no", "value": "v"}), "read it"),
        writer=_steps(ENABLE, ("fakeapp__poke", {"key": "yes", "value": "v"}), "done"),
    )
    outcome, provider = await _swarm(tmp_path, script, ctx=_context(env), integrations=factory)

    assert outcome.plan.lead == "writer"
    assert [a.id for a in outcome.plan.agents if a.can_act] == ["writer"]
    reader, lead = (
        [{t["function"]["name"] for t in r.tools} for r in provider.requests if _agent(r) == who]
        for who in ("researcher", "writer")
    )
    assert "integrations_enable" in reader[0] and "swarm_note_read" in reader[0]
    assert "fakeapp__peek" in reader[1] and "fakeapp__poke" not in reader[1]
    assert "fakeapp__poke" in lead[1]
    assert env.manager.get("fakeapp").ran == [("poke", {"key": "yes", "value": "v"})]
    refused = [m["content"] for r in provider.requests for m in r.messages if m["role"] == "tool"]
    assert any("this agent may not do" in content for content in refused)


async def test_the_dispatcher_cannot_get_a_change_past_a_non_lead(tmp_path):
    # Two apps over a budget of four tools: the second one is reached by integration_call.
    env, factory = _integrations(
        tmp_path, max_tools=4, connectors={"otherapp": OtherApp, "fakeapp": FakeApp}
    )
    other = ("integrations_enable", {"integration": "otherapp"})
    arguments = '{"key": "k", "value": "v"}'
    poke = (
        "integration_call",
        {"integration": "fakeapp", "operation": "poke", "arguments": arguments},
    )
    script = _script(INTEGRATIONS, researcher=_steps(other, ENABLE, poke, "refused"))
    outcome, provider = await _swarm(tmp_path, script, ctx=_context(env), integrations=factory)

    assert outcome.ok and env.manager.get("fakeapp").ran == []
    refused = [m["content"] for r in provider.requests for m in r.messages if m["role"] == "tool"]
    assert any("not allowed for this agent" in content for content in refused)


async def test_plan_mode_denies_integration_changes_to_the_lead_too(tmp_path):
    env, factory = _integrations(tmp_path)
    script = _script(
        INTEGRATIONS,
        writer=_steps(ENABLE, ("fakeapp__poke", {"key": "x", "value": "v"}), "proposed"),
    )
    outcome, _ = await _swarm(
        tmp_path, script, ctx=_context(env), integrations=factory, mode=ExecutionMode.PLAN
    )
    assert outcome.ok and env.manager.get("fakeapp").ran == []


async def test_a_failing_integration_source_leaves_the_scratch_tools(tmp_path):
    def broken(_task):
        raise RuntimeError("boom")

    outcome, provider = await _swarm(tmp_path, _answer(INTEGRATIONS), integrations=broken)
    assert outcome.ok
    assert "swarm_note_read" in _tools_for(provider, "writer")


async def test_a_run_without_integrations_says_so(tmp_path):
    outcome, _ = await _swarm(tmp_path, _answer(INTEGRATIONS))
    assert any("integrations are not offered" in note for note in outcome.plan.notes)


async def test_the_repl_gives_each_agent_its_own_source(tmp_path):
    from velune.cli.handlers.council import _swarm_integrations

    env = make_env(tmp_path)
    repl = SimpleNamespace(_resource_manager=env.manager, runtime=SimpleNamespace(config=None))
    factory = _swarm_integrations(repl, tmp_path)
    assert factory("a") is not factory("b")
    assert _swarm_integrations(SimpleNamespace(), tmp_path) is None


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
