"""Run a swarm: route, then run the plan's stages, then hand back the lead's result.

Stages run one after another. Agents inside a stage run at the same time, at
most ``SwarmSettings.parallel`` at once (1 for a local model server, which has
one GPU; more for cloud providers). All agents share one budget and one undo
journal, so one ``/rewind`` reverses every file change the run made.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from velune.permissions import ExecutionMode
from velune.swarm.agent import AgentEnv, run_agent
from velune.swarm.budget import BudgetedProvider, SwarmBudget
from velune.swarm.library import SkillLibrary
from velune.swarm.policy import SwarmPolicy
from velune.swarm.router import route
from velune.swarm.store import RunStore
from velune.swarm.types import AgentResult, SwarmPlan
from velune.tools.base.tool import ToolPermission
from velune.tools.filesystem.mutation import (
    TurnJournal,
    reset_active_journal,
    set_active_journal,
)

_MERMAID = re.compile(r"^```mermaid[ \t]*\r?\n(.*?)^```", re.MULTILINE | re.DOTALL)


@dataclass
class SwarmSettings:
    max_seconds: float = 600.0
    max_calls: int = 40
    max_tokens: int = 200_000
    turns_per_agent: int = 8
    parallel: int = 3  # agents at once on one provider; use 1 for local model servers


@dataclass
class SwarmOutcome:
    plan: SwarmPlan
    status: str  # ok | failed | stopped
    content: str  # the lead's result ("" unless ok)
    error: str  # why it failed or stopped
    results: list[AgentResult]
    run_id: str
    folder: Path
    journal: TurnJournal = field(default_factory=TurnJournal)
    files_changed: list[str] = field(default_factory=list)  # from successful tool calls
    warnings: list[str] = field(default_factory=list)  # what the run did not deliver
    diagram_path: Path | None = None  # the lead's mermaid blocks, saved in the run folder

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class SwarmUI(Protocol):
    def show_plan(self, plan: SwarmPlan) -> None: ...
    def stage_start(self, index: int, agent_ids: list[str], parallel: bool) -> None: ...
    def agent_event(self, agent_id: str, event: str, data: dict[str, Any]) -> None: ...
    def agent_done(self, result: AgentResult) -> None: ...


class NullUI:
    """For headless runs (``velune ask``, MCP): shows nothing."""

    def show_plan(self, plan: SwarmPlan) -> None:
        return None

    def stage_start(self, index: int, agent_ids: list[str], parallel: bool) -> None:
        return None

    def agent_event(self, agent_id: str, event: str, data: dict[str, Any]) -> None:
        return None

    def agent_done(self, result: AgentResult) -> None:
        return None


async def run_swarm(
    task: str,
    library: SkillLibrary,
    env: AgentEnv,
    *,
    runs_root: Path,
    settings: SwarmSettings | None = None,
    ui: SwarmUI | None = None,
    skill: str | None = None,
    permission: str | None = None,
    mode: ExecutionMode | None = None,
    journal: TurnJournal | None = None,
) -> SwarmOutcome:
    """Plan and run *task*. Raises ``RouteError`` for an unknown ``--skill``/``--permission``.

    Pass *journal* to keep hold of the run's file changes even when the run is
    cancelled (the caller publishes it for ``/rewind`` in its own ``finally``).
    """
    settings = settings or SwarmSettings()
    ui = ui or NullUI()
    budget = SwarmBudget(
        max_seconds=settings.max_seconds,
        max_calls=settings.max_calls,
        max_tokens=settings.max_tokens,
    )
    provider = BudgetedProvider(env.provider, budget)
    plan = await route(
        task,
        library,
        provider=provider,
        model_id=env.model_id,
        skill=skill,
        permission=permission,
    )
    for agent in plan.agents:
        agent.max_turns = settings.turns_per_agent
    if not env.tools:
        plan.notes.append(
            "This model can't call tools: agents answer from the repository summary; "
            "nothing is read or changed."
        )
    skill_obj = library.skill(plan.skill)
    profile = library.profile(plan.permission)
    assert skill_obj is not None and profile is not None  # route() only returns known names
    policy = SwarmPolicy.build(profile, mode=mode)
    if plan.output == "files" and not (policy.can_write and env.tools):
        plan.output = "diff"
        plan.notes.append("No files can be changed in this run: the changes are proposed instead.")
    if env.tools and env.integrations is None and ToolPermission.INTEGRATION_READ in policy.allowed:
        plan.notes.append(
            "Velune integrations are not offered in this run: they need /council inside "
            "`velune`, with resources.expose_to_model on."
        )
    store = RunStore.create(runs_root, plan)
    ui.show_plan(plan)

    agent_env = replace(env, provider=provider, on_event=ui.agent_event)
    gate = asyncio.Semaphore(max(1, settings.parallel))

    async def run_one(agent_id: str) -> AgentResult:
        spec = plan.agent(agent_id)
        async with gate:
            try:
                result = await asyncio.wait_for(
                    run_agent(spec, plan, skill_obj, profile, policy, store, agent_env),
                    timeout=max(1.0, budget.time_left()),
                )
            except TimeoutError:
                result = store.record(
                    AgentResult(
                        agent_id,
                        "timeout",
                        error=f"stopped at the run's time limit ({settings.max_seconds:.0f}s)",
                    )
                )
        ui.agent_done(result)
        return result

    journal = journal if journal is not None else TurnJournal()
    token = set_active_journal(journal)  # agent tasks inherit it: one journal per run
    stopped = ""
    try:
        for index, stage in enumerate(plan.stages):
            stopped = budget.exhausted_reason()
            if stopped:
                break
            ui.stage_start(index, stage, parallel=len(stage) > 1 and settings.parallel > 1)
            store.event("stage", stage=index, agents=stage)
            results = await asyncio.gather(*(run_one(agent_id) for agent_id in stage))
            if any(r.status == "budget" for r in results):
                stopped = budget.exhausted_reason() or "the run's budget was used up"
                break
    finally:
        reset_active_journal(token)

    lead = store.result(plan.lead)
    if lead is not None and lead.ok:
        status, content, error = "ok", lead.content, ""
    elif stopped:
        status, content, error = "stopped", "", f"Stopped: {stopped}."
    elif lead is None:
        status, content, error = "failed", "", f"{plan.lead} did not run."
    else:
        status, content, error = "failed", "", f"{lead.agent_id} failed: {lead.error}"
    results = [store.results[a.id] for a in plan.agents if a.id in store.results]
    files_changed = list(dict.fromkeys(f for r in results for f in r.files_touched))
    warnings: list[str] = []
    diagram_path = None
    if status == "ok" and plan.output == "files" and not files_changed:
        warnings.append("No files were changed.")
    if status == "ok" and plan.output == "diagram":
        diagram_path = _save_diagram(store.folder, content)
        if diagram_path is None:
            warnings.append("No mermaid diagram was found in the answer, so none was saved.")
    store.event("done", status=status, error=error, files=files_changed)
    return SwarmOutcome(
        plan=plan,
        status=status,
        content=content,
        error=error,
        results=results,
        run_id=store.run_id,
        folder=store.folder,
        journal=journal,
        files_changed=files_changed,
        warnings=warnings,
        diagram_path=diagram_path,
    )


def _save_diagram(folder: Path, content: str) -> Path | None:
    """Write the answer's mermaid blocks to ``diagram.mmd`` in the run folder."""
    blocks = [block.strip() for block in _MERMAID.findall(content) if block.strip()]
    if not blocks:
        return None
    path = folder / "diagram.mmd"
    try:
        path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    except OSError:
        return None
    return path
