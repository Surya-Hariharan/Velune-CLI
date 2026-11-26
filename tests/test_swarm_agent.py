"""Swarm S2: permission policy, run store and one agent end to end."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.swarm_fakes import (
    ReadTool,
    RecordingGate,
    ScriptedProvider,
    ShellTool,
    WriteTool,
    calls,
    registry,
    system_of,
    text,
    user_of,
)
from velune.orchestration.tool_loop import Denied, ToolLoopRunner
from velune.permissions import ExecutionMode
from velune.swarm.agent import AgentEnv, build_messages, run_agent
from velune.swarm.library import SkillLibrary
from velune.swarm.policy import SwarmPolicy
from velune.swarm.store import RunStore, prune_runs
from velune.swarm.types import AgentResult, AgentSpec, SwarmPlan
from velune.tools.base.tool import ToolCallContext, ToolPermission

LIBRARY = SkillLibrary.load(user_root=Path("does-not-exist"))


def _policy(profile: str, mode: ExecutionMode | None = None) -> SwarmPolicy:
    return SwarmPolicy.build(LIBRARY.profile(profile), mode=mode)


def _offered(policy: SwarmPolicy) -> set[str]:
    return set(policy.tool_registry(registry(ReadTool(), WriteTool(), ShellTool())).list_tools())


# ── policy ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("profile", "offered"),
    [
        ("answer-only", set()),
        ("read-only", {"read_file"}),
        ("propose-changes", {"read_file"}),
        ("edit-workspace", {"read_file", "write_file"}),
        ("build-project", {"read_file", "write_file", "execute_command"}),
    ],
)
def test_each_profile_offers_only_its_tools(profile, offered):
    assert _offered(_policy(profile)) == offered


def test_a_non_writer_loses_writes_and_commands():
    policy = _policy("build-project").for_agent(can_write=False)
    assert _offered(policy) == {"read_file"}
    assert policy.write == "none" and policy.commands == "none"


def test_plan_mode_removes_writes_and_commands_from_every_profile():
    policy = _policy("build-project", ExecutionMode.PLAN)
    assert _offered(policy) == {"read_file"}
    assert policy.write == "proposal"


def test_mcp_tools_are_only_permitted_by_a_profile_that_lists_mcp():
    network = {ToolPermission.NETWORK_ACCESS}
    assert not _policy("read-only").permits("github_create_issue", network)
    assert _policy("integrations").permits("github_create_issue", network)


async def test_the_approver_refuses_with_the_profile_as_the_reason():
    approve = _policy("read-only").approver()
    denied = await approve("write_file", {}, {ToolPermission.FILESYSTEM_WRITE})
    assert isinstance(denied, Denied) and not denied
    assert "'read-only' permission profile" in denied.reason
    assert await approve("read_file", {}, {ToolPermission.FILESYSTEM_READ}) is True


async def test_the_approver_enforces_the_call_limit_but_not_on_scratch_tools():
    approve = SwarmPolicy.build(LIBRARY.profile("read-only")).approver()
    limit = LIBRARY.profile("read-only").max_tool_calls
    for _ in range(limit):
        assert await approve("read_file", {}, {ToolPermission.FILESYSTEM_READ}) is True
    assert await approve("swarm_note_read", {}, set()) is True
    over = await approve("read_file", {}, {ToolPermission.FILESYSTEM_READ})
    assert isinstance(over, Denied) and "limit" in over.reason


async def test_the_approver_defers_to_the_base_approver():
    seen = []

    async def base(name, arguments, permissions):
        seen.append(name)
        return False

    approve = _policy("read-only").approver(base)
    assert await approve("read_file", {}, {ToolPermission.FILESYSTEM_READ}) is False
    assert seen == ["read_file"]


async def test_the_tool_loop_reports_a_denial_reason_to_the_model():
    provider = ScriptedProvider([calls(("write_file", {"file_path": "a"})), text("done")])

    async def refuse(name, arguments, permissions):
        return Denied("not in this task")

    runner = ToolLoopRunner(provider, registry(WriteTool()), approver=refuse)
    from velune.core.types.inference import InferenceRequest

    result = await runner.run(InferenceRequest(model_id="m", messages=[]))
    assert result.invocations[0].result == "Error: not in this task"


# ── run store ────────────────────────────────────────────────────────────────


def _plan(**overrides) -> SwarmPlan:
    agents = [
        AgentSpec("explorer", "explorer", "find the files", stage=0),
        AgentSpec("implementer", "implementer", "make the change", stage=1, can_write=True),
        AgentSpec("tester", "tester", "write tests", stage=1, can_write=True),
        AgentSpec("reviewer", "reviewer", "check and merge", stage=2),
    ]
    values = {
        "task": "add gc_content",
        "skill": "coding",
        "permission": "edit-workspace",
        "output": "diff",
        "lead": "reviewer",
        "agents": agents,
        "stages": [["explorer"], ["implementer", "tester"], ["reviewer"]],
    }
    values.update(overrides)
    return SwarmPlan(**values)


def test_the_store_keeps_results_notes_and_files_on_disk(tmp_path):
    store = RunStore.create(tmp_path / "runs", _plan())
    store.record(AgentResult("explorer", "ok", content="found x.py"))
    store.write_note("files", "x.py, y.py")
    assert (store.folder / "plan.json").is_file()
    assert (store.folder / "explorer.md").read_text(encoding="utf-8") == "found x.py"
    assert store.read_note("files") == "x.py, y.py"
    assert [r.agent_id for r in store.earlier_results(1)] == ["explorer"]
    assert store.earlier_results(0) == []


@pytest.mark.parametrize("name", ["../escape", "a/b", "", "x" * 65, ".hidden"])
def test_a_note_cannot_leave_the_run_folder(tmp_path, name):
    store = RunStore.create(tmp_path / "runs", _plan())
    with pytest.raises(ValueError):
        store.write_note(name, "text")
    assert not (tmp_path / "escape.md").exists()


def test_old_runs_are_pruned(tmp_path):
    import os
    import time

    runs = tmp_path / "runs"
    for i in range(25):
        folder = runs / f"run{i:02d}"
        folder.mkdir(parents=True)
        os.utime(folder, (time.time() - 1000 + i, time.time() - 1000 + i))
    stale = runs / "ancient"
    stale.mkdir()
    os.utime(stale, (time.time() - 30 * 86400,) * 2)
    prune_runs(runs)
    left = sorted(p.name for p in runs.iterdir())
    assert left == [f"run{i:02d}" for i in range(5, 25)]


# ── messages ─────────────────────────────────────────────────────────────────


def _messages(spec_id: str, store: RunStore, *, profile="edit-workspace", **kwargs):
    plan = store.plan
    spec = plan.agent(spec_id)
    policy = _policy(profile).for_agent(can_write=spec.can_write)
    return build_messages(
        spec, plan, LIBRARY.skill("coding"), LIBRARY.profile(profile), policy, store, **kwargs
    )


def test_the_lead_gets_the_output_contract_and_others_do_not(tmp_path):
    store = RunStore.create(tmp_path / "runs", _plan())
    assert "You write the final result" in _messages("reviewer", store)[0]["content"]
    assert "You write the final result" not in _messages("explorer", store)[0]["content"]


def test_a_non_writer_is_told_it_is_read_only(tmp_path):
    store = RunStore.create(tmp_path / "runs", _plan())
    assert "You are read-only in this task" in _messages("explorer", store)[0]["content"]
    assert "You are read-only in this task" not in _messages("implementer", store)[0]["content"]


def test_later_agents_see_earlier_results_and_failures(tmp_path):
    store = RunStore.create(tmp_path / "runs", _plan())
    store.record(AgentResult("explorer", "error", error="RateLimitError: slow down"))
    user = _messages("implementer", store)[1]["content"]
    assert "explorer (explorer) failed: error: RateLimitError: slow down" in user
    store.record(AgentResult("implementer", "ok", content="changed x.py"))
    assert "changed x.py" in _messages("reviewer", store)[1]["content"]


def test_repository_context_is_trimmed_before_the_task(tmp_path):
    store = RunStore.create(tmp_path / "runs", _plan(task="T" * 3000))
    user = _messages("explorer", store, context="C" * 100_000, context_window=8_000)[1]
    assert "T" * 3000 in user["content"]
    assert "(repository context trimmed)" in user["content"]
    assert len(user["content"]) < 14_000


# ── one agent end to end ─────────────────────────────────────────────────────


async def _run(store, spec_id, provider, *, profile="edit-workspace", ctx=None, tools=None):
    plan = store.plan
    env = AgentEnv(
        provider=provider,
        model_id="fake",
        registry=tools or registry(ReadTool(), WriteTool()),
        ctx=ctx,
    )
    return await run_agent(
        plan.agent(spec_id),
        plan,
        LIBRARY.skill("coding"),
        LIBRARY.profile(profile),
        _policy(profile),
        store,
        env,
    )


async def test_an_agent_reads_a_file_then_answers(tmp_path):
    store = RunStore.create(tmp_path / "runs", _plan())
    provider = ScriptedProvider(
        [calls(("read_file", {"file_path": "dna.py"})), text("dna.py has gc_content stub")]
    )
    result = await _run(store, "explorer", provider)
    assert result.ok and result.content == "dna.py has gc_content stub"
    assert result.tool_calls == ["read_file(dna.py)"]
    assert store.result("explorer") is result
    assert "contents of dna.py" in str(provider.requests[1].messages)


async def test_a_writer_change_goes_through_the_gate(tmp_path):
    store = RunStore.create(tmp_path / "runs", _plan())
    gate = RecordingGate(tmp_path)
    ctx = ToolCallContext(run_id="r", actor="swarm", workspace=tmp_path, gate=gate)
    writer = WriteTool()
    provider = ScriptedProvider(
        [calls(("write_file", {"file_path": "dna.py", "content": "x"})), text("done")]
    )
    result = await _run(store, "implementer", provider, ctx=ctx, tools=registry(writer))
    assert gate.checked == ["write_file"]
    assert writer.written == {"dna.py": "x"}
    assert result.files_touched == ["dna.py"]


async def test_without_a_gate_a_write_fails_closed(tmp_path):
    store = RunStore.create(tmp_path / "runs", _plan())
    writer = WriteTool()
    provider = ScriptedProvider(
        [calls(("write_file", {"file_path": "dna.py", "content": "x"})), text("could not write")]
    )
    result = await _run(store, "implementer", provider, tools=registry(writer))
    assert writer.written == {}
    assert result.files_touched == []
    assert "no permission gate is active" in str(provider.requests[1].messages)


async def test_a_read_only_agent_is_not_even_offered_write_tools(tmp_path):
    store = RunStore.create(tmp_path / "runs", _plan(permission="read-only"))
    provider = ScriptedProvider([text("only read")])
    await _run(store, "implementer", provider, profile="read-only")
    offered = {t["function"]["name"] for t in provider.requests[0].tools or []}
    assert "write_file" not in offered and "read_file" in offered
    assert {"swarm_note_write", "swarm_note_read", "swarm_result_read"} <= offered


async def test_agents_share_notes_and_full_results(tmp_path):
    store = RunStore.create(tmp_path / "runs", _plan())
    first = ScriptedProvider(
        [calls(("swarm_note_write", {"name": "files", "text": "dna.py"})), text("found dna.py")]
    )
    await _run(store, "explorer", first)
    second = ScriptedProvider(
        [
            calls(
                ("swarm_note_read", {"name": "files"}),
                ("swarm_result_read", {"agent_id": "explorer"}),
            ),
            text("ok"),
        ]
    )
    await _run(store, "implementer", second)
    tool_results = [m["content"] for m in second.requests[1].messages if m["role"] == "tool"]
    assert tool_results == ["dna.py", "found dna.py"]


async def test_a_provider_error_is_recorded_with_its_real_message(tmp_path):
    from velune.core.errors.provider import RateLimitError

    store = RunStore.create(tmp_path / "runs", _plan())
    provider = ScriptedProvider([RateLimitError("rate limit reached for gpt-oss-120b")])
    result = await _run(store, "explorer", provider)
    assert result.status == "error"
    assert "RateLimitError" in result.error and "rate limit reached" in result.error


async def test_an_empty_answer_is_a_failure_not_a_result(tmp_path):
    store = RunStore.create(tmp_path / "runs", _plan())
    result = await _run(store, "explorer", ScriptedProvider([text("   ")]))
    assert result.status == "error" and result.error == "the model returned no text"


async def test_the_agent_prompt_carries_role_and_task(tmp_path):
    store = RunStore.create(tmp_path / "runs", _plan())
    provider = ScriptedProvider([text("ok")])
    await _run(store, "tester", provider)
    assert "# Your role: tester" in system_of(provider.requests[0])
    assert "# Your task\nwrite tests" in user_of(provider.requests[0])
