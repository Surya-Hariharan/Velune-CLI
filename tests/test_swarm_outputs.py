"""S5: what each output type really delivers (files, build-project, diagram)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import toml

from tests.integration_fakes import FakeApp, OtherApp, make_env
from tests.swarm_fakes import (
    FakeContainer,
    ReadTool,
    RecordingGate,
    ScriptedProvider,
    ShellTool,
    calls,
    descriptor,
    fake_repl,
    registry,
    system_of,
    text,
)
from tests.test_swarm_run import CODING, LIBRARY, _agent, _answer, _route, _swarm
from velune.cli.display.swarm_view import plan_table
from velune.kernel.config import ConfigLoader
from velune.permissions import ExecutionMode
from velune.permissions.gate import Approval
from velune.swarm.agent import (
    CHECK_FILES_CONTRACT,
    OUTPUT_CONTRACTS,
    AgentEnv,
    ModelChoice,
    _touched,
)
from velune.swarm.run import SwarmSettings, run_swarm
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


# ── a model per role ─────────────────────────────────────────────────────────


def _choice(provider, name, **values):
    return ModelChoice(f"{provider.provider_id}/{name}", provider=provider, model_id=name, **values)


def _other(provider_id="other", *, script=None, delay=0.0):
    provider = ScriptedProvider(script or _answer(CODING), delay=delay)
    provider.provider_id = provider_id
    return provider


async def _models_run(tmp_path, role_models, *, delay=0.0, settings=None):
    main = ScriptedProvider(_answer(CODING), delay=delay)
    env = AgentEnv(
        provider=main, model_id="chat", registry=registry(ReadTool()), role_models=role_models
    )
    outcome = await run_swarm(
        "add gc_content",
        LIBRARY,
        env,
        runs_root=tmp_path / "runs",
        settings=settings or SwarmSettings(),
    )
    return outcome, main


def _asked(provider) -> dict[str, str]:
    return {_agent(r): r.model_id for r in provider.requests}


async def test_a_role_model_wins_over_the_lead_model_and_the_rest_use_the_run_model(tmp_path):
    other = _other()
    outcome, main = await _models_run(
        tmp_path,
        {
            "explorer": _choice(other, "small"),
            "reviewer": _choice(other, "judge"),
            "lead": _choice(other, "unused"),
        },
    )
    assert outcome.ok
    assert _asked(other) == {"explorer": "small", "reviewer": "judge"}
    assert set(_asked(main).values()) == {"chat"} and "router" in _asked(main)
    saved = json.loads((outcome.folder / "plan.json").read_text(encoding="utf-8"))
    models = {a["id"]: a["model"] for a in saved["agents"]}
    assert models["reviewer"] == "other/judge" and models["tester"] == ""
    assert "Model" in [c.header for c in plan_table(outcome.plan).columns]


async def test_the_lead_model_applies_to_whoever_leads(tmp_path):
    other = _other()
    outcome, _ = await _models_run(tmp_path, {"lead": _choice(other, "judge")})
    assert _asked(other) == {outcome.plan.lead: "judge"}


async def test_an_unusable_role_model_is_noted_and_replaced(tmp_path):
    broken = ModelChoice("openai/nope", problem="is not available right now")
    outcome, main = await _models_run(tmp_path, {"reviewer": broken})
    assert outcome.ok and _asked(main)["reviewer"] == "chat"
    assert any("'openai/nope' for 'reviewer'" in note for note in outcome.plan.notes)
    assert "Model" not in [c.header for c in plan_table(outcome.plan).columns]


async def test_role_models_share_the_run_budget(tmp_path):
    other = _other()
    settings = SwarmSettings(max_calls=2)  # the router and one agent
    outcome, main = await _models_run(
        tmp_path, {"reviewer": _choice(other, "judge")}, settings=settings
    )
    assert outcome.status == "stopped" and other.requests == []
    assert len(main.requests) == 2


@pytest.mark.parametrize(("local", "together"), [(True, 1), (False, 2)])
async def test_local_servers_share_one_limit_and_cloud_providers_do_not(tmp_path, local, together):
    running = {"now": 0, "most": 0}

    class Counting(ScriptedProvider):
        async def infer(self, request):
            if _agent(request) in ("implementer", "tester"):
                running["now"] += 1
                running["most"] = max(running["most"], running["now"])
                try:
                    return await super().infer(request)
                finally:
                    running["now"] -= 1
            return await super().infer(request)

    first, second = Counting(_answer(CODING), delay=0.05), Counting(_answer(CODING), delay=0.05)
    first.provider_id, second.provider_id = "a", "b"
    settings = SwarmSettings(parallel_cloud=1, parallel_local=1)
    outcome, _ = await _models_run(
        tmp_path,
        {
            "implementer": _choice(first, "x", local=local),
            "tester": _choice(second, "y", local=local),
        },
        settings=settings,
    )
    assert outcome.ok and running["most"] == together


async def test_files_are_proposed_when_no_writer_can_call_tools(tmp_path):
    _, ctx, tools = _workspace(tmp_path)
    other = _other(script=_answer(BUILD))
    blind = _choice(other, "blind", tools=False)
    outcome, _ = await _swarm(
        tmp_path,
        _answer(BUILD),
        tools=tools,
        ctx=ctx,
        role_models={"implementer": blind, "tester": blind},
    )
    assert outcome.plan.output == "diff"
    assert any("can't call tools" in note for note in outcome.plan.notes)


def _models_repl(tmp_path, monkeypatch):
    from velune.cli.handlers import council
    from velune.kernel.config import VeluneConfig
    from velune.swarm import setup

    monkeypatch.setattr(setup, "load_library", lambda *_a, **_k: LIBRARY)
    repl = fake_repl(tmp_path, ScriptedProvider([]))
    known = {("ollama", "judge"): descriptor("judge", "ollama")}
    repl.container.services.update(
        {
            "runtime.config": VeluneConfig(),
            "runtime.config_path": tmp_path / "velune.toml",
            "runtime.model_registry": SimpleNamespace(get=lambda m, p: known.get((p, m))),
            "runtime.provider_registry": SimpleNamespace(get=lambda _id: object()),
        }
    )
    return council, repl


async def test_council_models_sets_lists_and_clears(tmp_path, monkeypatch):
    council, repl = _models_repl(tmp_path, monkeypatch)
    config = repl.container.services["runtime.config"]
    saved = tmp_path / "velune.toml"

    await council.cmd_council(repl, "models set Reviewer ollama/judge")
    assert config.swarm.models == {"reviewer": "ollama/judge"}
    assert toml.load(saved)["swarm"]["models"] == {"reviewer": "ollama/judge"}
    assert ConfigLoader(saved).load().swarm.models == {"reviewer": "ollama/judge"}

    await council.cmd_council(repl, "models")
    assert "ready" in repl.output.getvalue()

    await council.cmd_council(repl, "models clear reviewer")
    assert config.swarm.models == {} and toml.load(saved)["swarm"].get("models", {}) == {}


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ("models set nobody ollama/judge", "No role named 'nobody'"),
        ("models set reviewer ollama/missing", "is not a known model"),
        ("models set reviewer judge", "is not a known model"),
        ("models set reviewer", "Usage: /council models"),
        ("models clear tester", "No model is set for 'tester'"),
    ],
)
async def test_council_models_refuses_bad_input(tmp_path, monkeypatch, args, message):
    council, repl = _models_repl(tmp_path, monkeypatch)
    await council.cmd_council(repl, args)
    assert message in repl.output.getvalue()
    assert not (tmp_path / "velune.toml").exists()


@pytest.mark.parametrize(
    "task", ["models are slow, find out why", "models set up the config please"]
)
async def test_a_task_that_starts_with_models_still_runs(tmp_path, monkeypatch, task):
    from velune.cli.handlers import council

    ran = []

    async def fake_run(_repl, text_, **_kwargs):
        ran.append(text_)

    monkeypatch.setattr(council, "execute_council_task", fake_run)
    await council.cmd_council(fake_repl(tmp_path, ScriptedProvider([])), task)
    assert ran == [task]


def test_configured_models_resolve_to_a_provider_or_a_reason():
    from velune.swarm.setup import role_models

    provider = object()
    container = FakeContainer(
        {
            "runtime.model_registry": SimpleNamespace(
                get=lambda m, p: descriptor("judge", "ollama") if m == "judge" else None
            ),
            "runtime.provider_registry": SimpleNamespace(get=lambda _id: provider),
        }
    )
    config = SimpleNamespace(
        swarm=SimpleNamespace(models={"Reviewer": "ollama/judge", "lead": "ollama/gone"})
    )
    found = role_models(container, config, lambda _m, _p: False)
    assert found["reviewer"].provider is provider and found["reviewer"].local
    assert found["reviewer"].tools is False and not found["reviewer"].problem
    assert "not a known model" in found["lead"].problem


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
