"""Swarm S3a: router, stage loop, writers, budget, shared journal, failures."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from tests.swarm_fakes import (
    ReadTool,
    RecordingGate,
    ScriptedProvider,
    calls,
    registry,
    system_of,
    text,
    user_of,
)
from velune.core.types.inference import InferenceRequest
from velune.swarm.agent import AgentEnv
from velune.swarm.library import SkillLibrary
from velune.swarm.router import RouteError, first_json_object, route
from velune.swarm.run import SwarmSettings, run_swarm
from velune.tools.base.tool import ToolCallContext

LIBRARY = SkillLibrary.load(user_root=Path("does-not-exist"))


def _agent(request: InferenceRequest) -> str:
    """Which agent sent *request* ("router" for the routing call)."""
    found = re.search(r"\('([a-z0-9-]+)'\)", system_of(request))
    return found.group(1) if found else "router"


def _answer(plan: dict | None = None, **replies):
    """A script: the router returns *plan*; agent X returns replies[X] (default "X done")."""

    def script(request: InferenceRequest):
        who = _agent(request)
        if who == "router":
            return text(json.dumps(plan) if plan is not None else "not json")
        reply = replies.get(who.replace("-", "_"), f"{who} done")
        return reply if not isinstance(reply, str) else text(reply)

    return script


async def _route(answer: str | Exception | list, **kwargs):
    script = answer if isinstance(answer, list) else [answer]
    provider = ScriptedProvider([a if isinstance(a, Exception) else text(a) for a in script])
    plan = await route("add gc_content", LIBRARY, provider=provider, model_id="m", **kwargs)
    return plan, provider


CODING = {
    "skill": "coding",
    "permission": "propose-changes",
    "output": "diff",
    "agents": [
        {"id": "explorer", "role": "explorer", "task": "find dna.py"},
        {"id": "implementer", "role": "implementer", "task": "add gc_content"},
        {"id": "tester", "role": "tester", "task": "write tests"},
        {"id": "reviewer", "role": "reviewer", "task": "merge"},
    ],
    "flow": "explorer > implementer, tester > reviewer",
    "lead": "reviewer",
}


# ── router ───────────────────────────────────────────────────────────────────


def test_first_json_object_skips_prose_fences_and_braces_in_strings():
    raw = 'Sure! ```json\n{"a": "x } y", "b": {"c": 1}}\n``` then {"d": 2}'
    assert first_json_object(raw) == {"a": "x } y", "b": {"c": 1}}
    with pytest.raises(RouteError):
        first_json_object("no object here")


async def test_the_router_assigns_each_agent_its_own_task():
    plan, _ = await _route("Here is the plan:\n" + json.dumps(CODING))
    assert plan.skill == "coding" and plan.permission == "propose-changes"
    assert plan.stages == [["explorer"], ["implementer", "tester"], ["reviewer"]]
    assert {a.id: a.task for a in plan.agents}["implementer"] == "add gc_content"
    assert plan.notes == []


async def test_the_router_retries_once_then_falls_back_to_the_default_team():
    plan, provider = await _route(["junk", json.dumps(CODING)])
    assert len(provider.requests) == 2 and plan.agent("explorer").task == "find dna.py"
    assert "Reply with only the JSON object" in str(provider.requests[1].messages[-1])

    plan, _ = await _route(["junk", "more junk"])
    assert [a.task for a in plan.agents] == ["add gc_content"] * len(plan.agents)
    assert any("default team" in note for note in plan.notes)


async def test_a_failing_router_call_falls_back_and_says_why():
    from velune.core.errors.provider import RateLimitError

    plan, _ = await _route(RateLimitError("slow down"))
    assert any("RateLimitError: slow down" in note for note in plan.notes)
    assert plan.agents  # still a usable plan


async def test_one_role_twice_needs_distinct_ids():
    review = {
        "skill": "review",
        "agents": [
            {"id": "review-cli", "role": "correctness", "task": "cli/"},
            {"id": "review-core", "role": "correctness", "task": "core/"},
            {"id": "lead", "role": "lead", "task": "merge"},
        ],
        "flow": "review-cli, review-core > lead",
        "lead": "lead",
    }
    plan, _ = await _route(json.dumps(review))
    assert [a.id for a in plan.agents] == ["review-cli", "review-core", "lead"]
    assert plan.stages == [["review-cli", "review-core"], ["lead"]]

    for agent in review["agents"][:2]:
        del agent["id"]
    plan, _ = await _route(json.dumps(review))
    assert any("used twice" in note for note in plan.notes)
    assert [a.id for a in plan.agents] == ["mapper", "correctness", "security", "lead"]


async def test_read_only_downgrades_a_diff_to_text():
    plan, _ = await _route(json.dumps(CODING), skill="coding", permission="read-only")
    assert plan.permission == "read-only" and plan.output == "text"
    assert any("shown, not applied" in note for note in plan.notes)
    assert not any(a.can_write for a in plan.agents)


async def test_the_router_cannot_pick_a_project_profile(tmp_path):
    folder = tmp_path / "repo" / ".velune" / "permissions"
    folder.mkdir(parents=True)
    (folder / "wide.md").write_text(
        "---\ntools: [read, write]\nwrite: workspace\ncommands: none\n---\nx\n", encoding="utf-8"
    )
    library = SkillLibrary.load(
        workspace=tmp_path / "repo", trusted=True, user_root=Path("does-not-exist")
    )
    provider = ScriptedProvider([text(json.dumps({**CODING, "permission": "wide"}))])
    plan = await route("x", library, provider=provider, model_id="m")
    assert plan.permission == "propose-changes"
    assert any("only used when you name it" in note for note in plan.notes)
    named = await route("x", library, permission="wide")
    assert named.permission == "wide"


async def test_an_unknown_skill_or_profile_named_by_the_user_is_an_error():
    with pytest.raises(RouteError, match="no skill named 'nope'"):
        await route("x", LIBRARY, skill="nope")
    with pytest.raises(RouteError, match="no permission profile named 'nope'"):
        await route("x", LIBRARY, permission="nope")


async def test_writers_never_share_a_stage():
    plan = await route("add gc_content", LIBRARY, skill="coding", permission="edit-workspace")
    assert plan.stages == [["explorer"], ["implementer"], ["tester"], ["reviewer"]]
    assert {a.id for a in plan.agents if a.can_write} == {"implementer", "tester"}
    assert any("run one after another" in note for note in plan.notes)


async def test_the_lead_is_moved_to_run_last_on_its_own():
    plan, _ = await _route(
        json.dumps({**CODING, "flow": "explorer > implementer, tester, reviewer"})
    )
    assert plan.stages[-1] == ["reviewer"]
    assert any("run last" in note for note in plan.notes)


async def test_the_career_question_routes_to_research_without_tools():
    question = "how valid is this project for my AI and bioinformatics career path"
    plan = await route(question, LIBRARY)
    assert (plan.skill, plan.permission, plan.output) == ("research", "answer-only", "text")


# ── running ──────────────────────────────────────────────────────────────────


async def _swarm(
    tmp_path, script, *, parallel=3, delay=0.0, tools=None, ctx=None, integrations=None, **kwargs
):
    provider = ScriptedProvider(script, delay=delay)
    env = AgentEnv(
        provider=provider,
        model_id="m",
        registry=tools or registry(ReadTool()),
        ctx=ctx,
        integrations=integrations,
    )
    settings = kwargs.pop("settings", SwarmSettings(parallel=parallel))
    outcome = await run_swarm(
        "add gc_content", LIBRARY, env, runs_root=tmp_path / "runs", settings=settings, **kwargs
    )
    return outcome, provider


@pytest.mark.parametrize(("parallel", "at_once"), [(3, 2), (1, 1)])
async def test_same_stage_agents_run_together_only_when_allowed(tmp_path, parallel, at_once):
    outcome, provider = await _swarm(tmp_path, _answer(CODING), parallel=parallel, delay=0.05)
    assert outcome.ok and outcome.content == "reviewer done"
    assert provider.max_active == at_once


async def test_the_next_stage_sees_what_earlier_agents_found(tmp_path):
    outcome, provider = await _swarm(
        tmp_path, _answer(CODING, explorer="dna.py holds the stub", implementer="added it")
    )
    reviewer = next(r for r in provider.requests if _agent(r) == "reviewer")
    assert "dna.py holds the stub" in user_of(reviewer) and "added it" in user_of(reviewer)
    tester = next(r for r in provider.requests if _agent(r) == "tester")
    assert "added it" not in user_of(tester)  # same stage: not visible


async def test_a_failed_agent_is_reported_to_later_agents(tmp_path):
    from velune.core.errors.provider import RateLimitError

    outcome, provider = await _swarm(
        tmp_path, _answer(CODING, explorer=RateLimitError("rate limited"))
    )
    implementer = next(r for r in provider.requests if _agent(r) == "implementer")
    assert "explorer (explorer) failed: error: RateLimitError: rate limited" in user_of(implementer)
    assert outcome.ok


async def test_a_failed_lead_fails_the_run_with_its_real_error(tmp_path):
    from velune.core.errors.provider import ProviderAuthenticationError

    outcome, _ = await _swarm(
        tmp_path, _answer(CODING, reviewer=ProviderAuthenticationError("invalid api key"))
    )
    assert outcome.status == "failed"
    assert "reviewer failed: ProviderAuthenticationError" in outcome.error
    assert "invalid api key" in outcome.error


async def test_parallel_agents_cannot_exceed_the_call_budget(tmp_path):
    settings = SwarmSettings(max_calls=3, parallel=3)
    outcome, provider = await _swarm(tmp_path, _answer(CODING), settings=settings)
    assert len(provider.requests) == 3  # router + explorer + one of the two stage-2 agents
    assert outcome.status == "stopped" and "model-call limit (3)" in outcome.error
    statuses = sorted(r.status for r in outcome.results)
    assert statuses == ["budget", "ok", "ok"]


async def test_one_journal_holds_every_stage_s_file_changes(tmp_path):
    from velune.tools.filesystem.write import WriteFile

    workspace = tmp_path / "ws"
    workspace.mkdir()
    gate = RecordingGate(workspace)
    ctx = ToolCallContext(run_id="r", actor="swarm", workspace=workspace, gate=gate)
    tools = registry(ReadTool(), WriteFile(workspace=workspace, confirm=False))

    def writes(path: str):
        state = {"done": False}

        def reply():
            if state["done"]:
                return text(f"wrote {path}")
            state["done"] = True
            return calls(("write_file", {"file_path": path, "content": "x = 1\n"}))

        return reply

    implementer, tester = writes("dna.py"), writes("test_dna.py")
    plan = {**CODING, "permission": "edit-workspace", "output": "files"}

    def script(request):
        who = _agent(request)
        if who == "implementer":
            return implementer()
        if who == "tester":
            return tester()
        return _answer(plan)(request)

    outcome, _ = await _swarm(tmp_path, script, tools=tools, ctx=ctx)
    assert outcome.ok
    assert outcome.plan.stages == [["explorer"], ["implementer"], ["tester"], ["reviewer"]]
    assert (workspace / "dna.py").is_file() and (workspace / "test_dna.py").is_file()
    assert gate.checked == ["write_file", "write_file"]
    report = outcome.journal.rewind()
    assert not (workspace / "dna.py").exists() and not (workspace / "test_dna.py").exists()
    assert report is not None


async def test_cancelling_keeps_the_journal_with_the_caller(tmp_path):
    from velune.tools.filesystem.mutation import _ACTIVE_JOURNAL, TurnJournal

    journal = TurnJournal()
    provider = ScriptedProvider(_answer(CODING), delay=0.2)
    env = AgentEnv(provider=provider, model_id="m", registry=registry(ReadTool()))
    task = asyncio.ensure_future(
        run_swarm("x", LIBRARY, env, runs_root=tmp_path / "runs", journal=journal)
    )
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _ACTIVE_JOURNAL.get() is None  # reset on the way out


async def test_the_run_is_saved_for_later(tmp_path):
    outcome, _ = await _swarm(tmp_path, _answer(CODING))
    assert (outcome.folder / "plan.json").is_file()
    assert (outcome.folder / "reviewer.md").read_text(encoding="utf-8") == "reviewer done"
    events = (outcome.folder / "events.jsonl").read_text(encoding="utf-8")
    assert '"event": "done"' in events and '"status": "ok"' in events
