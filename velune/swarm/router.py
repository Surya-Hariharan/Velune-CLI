"""Choose a skill, a permission profile and a team, and give each agent a task.

One model call proposes ``{skill, permission, output, agents, flow, lead}``;
everything it says is then checked in code. Anything invalid is corrected or
replaced by the skill's own defaults, and every correction is noted on the plan
so the user sees it. A choice the user made (``--skill``/``--permission``)
always wins over the model's.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from velune.core.types.inference import InferenceRequest
from velune.swarm.library import (
    _NAME_RE,
    MAX_AGENTS,
    OUTPUTS,
    LibraryError,
    PermissionProfile,
    Skill,
    SkillLibrary,
    parse_flow,
)
from velune.swarm.types import AgentSpec, SwarmPlan

logger = logging.getLogger("velune.swarm.router")

#: Outputs each profile write mode can deliver (see the plan's compatibility table).
ALLOWED_OUTPUTS = {
    "none": ("text", "plan", "diagram"),
    "proposal": ("text", "plan", "diagram", "diff"),
    "workspace": OUTPUTS,
}
FALLBACK_SKILL = "research"


class RouteError(ValueError):
    """The router's answer could not be turned into a valid plan."""


def first_json_object(text: str) -> dict[str, Any]:
    """The first complete ``{...}`` object in *text* (prose and code fences allowed)."""
    start = text.find("{")
    while start != -1:
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start : index + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(value, dict):
                        return value
                    break
        start = text.find("{", start + 1)
    raise RouteError("no JSON object found in the router's answer")


def router_prompt(task: str, library: SkillLibrary, skill: Skill | None) -> list[dict[str, str]]:
    skills = [skill] if skill else list(library.skills.values())
    skill_lines = "\n".join(
        f"- {s.slug}: {s.description}. Team: {', '.join(s.team)}. "
        f"Default flow: {' > '.join(', '.join(stage) for stage in s.flow)}. "
        f"Lead: {s.lead}. Output: {s.output}. Max agents: {s.max_agents}."
        for s in skills
    )
    profile_lines = "\n".join(
        f"- {p.slug}: {p.description} (writes: {p.write})" for p in library.auto_profiles()
    )
    system = f"""You plan how a small team of AI agents will handle a user's task.

Skills (pick one):
{skill_lines}

Permission profiles (pick the least powerful one that can deliver what the user asked for;
choose a profile that writes files only when the user clearly asked for changes to be made):
{profile_lines}

Outputs: text, plan, diff (proposed code edits), files (create or change files), diagram.

Reply with only a JSON object:
{{"skill": "<slug>", "permission": "<slug>", "output": "<output>",
  "agents": [{{"id": "<unique-id>", "role": "<role from the skill's team>", "task": "<this agent's own sub-task>"}}],
  "flow": "<ids: '>' means then, ',' means at the same time>",
  "lead": "<id of the agent that writes the final result>"}}

Rules: use only roles from the chosen skill's team; at most that skill's max agents; give every
agent a specific sub-task of the user's task; ids are lowercase letters, digits and '-', and a role
used twice needs two different ids; put every agent in the flow exactly once; the lead runs alone
in the last stage. Drop roles the task does not need."""
    return [{"role": "system", "content": system}, {"role": "user", "content": task}]


async def route(
    task: str,
    library: SkillLibrary,
    *,
    provider: Any | None = None,
    model_id: str = "",
    skill: str | None = None,
    permission: str | None = None,
) -> SwarmPlan:
    """Build a validated plan. With no provider, the skill's defaults are used."""
    notes: list[str] = []
    chosen_skill = _explicit_skill(library, skill)
    chosen_profile = _explicit_profile(library, permission)

    answer: dict[str, Any] = {}
    if provider is not None:
        messages = router_prompt(task, library, chosen_skill)
        for attempt in (1, 2):
            try:
                response = await provider.infer(
                    InferenceRequest(
                        model_id=model_id, messages=messages, temperature=0.1, max_tokens=1200
                    )
                )
                answer = first_json_object(response.content or "")
                break
            except asyncio.CancelledError:
                raise
            except RouteError as exc:
                if attempt == 2:
                    notes.append(f"Router answer unusable ({exc}); using the skill's default team.")
                    break
                messages = [
                    *messages,
                    {"role": "assistant", "content": response.content or ""},
                    {"role": "user", "content": f"{exc}. Reply with only the JSON object."},
                ]
            except Exception as exc:  # the model call itself failed
                notes.append(
                    f"Router call failed ({type(exc).__name__}: {exc}); "
                    "using the skill's default team."
                )
                break

    if chosen_skill is None:
        chosen_skill = _pick_skill(library, task, answer.get("skill"), notes)
    if chosen_profile is None:
        chosen_profile = _pick_profile(library, chosen_skill, answer.get("permission"), notes)
    try:
        plan = _plan_from_answer(task, chosen_skill, chosen_profile, answer, notes)
    except (RouteError, LibraryError) as exc:
        if answer:
            notes.append(f"Router team not usable ({exc}); using the skill's default team.")
        plan = _plan_from_answer(task, chosen_skill, chosen_profile, {}, notes)
    return plan


# ── choosing skill and profile ───────────────────────────────────────────────


def _explicit_skill(library: SkillLibrary, slug: str | None) -> Skill | None:
    if not slug:
        return None
    found = library.skill(slug)
    if found is None:
        raise RouteError(f"there is no skill named '{slug}' (see /skills)")
    return found


def _explicit_profile(library: SkillLibrary, slug: str | None) -> PermissionProfile | None:
    if not slug:
        return None
    found = library.profile(slug)
    if found is None:
        raise RouteError(f"there is no permission profile named '{slug}' (see /skills)")
    return found  # named by the user, so any source is allowed


def _pick_skill(library: SkillLibrary, task: str, proposed: Any, notes: list[str]) -> Skill:
    if isinstance(proposed, str) and library.skill(proposed):
        return library.skill(proposed)  # type: ignore[return-value]
    if proposed:
        notes.append(f"Router chose unknown skill '{proposed}'.")
    matching = [s for s in library.skills.values() if s.matches(task)]
    if matching:
        return matching[0]
    return library.skill(FALLBACK_SKILL) or next(iter(library.skills.values()))


def _pick_profile(
    library: SkillLibrary, skill: Skill, proposed: Any, notes: list[str]
) -> PermissionProfile:
    allowed = {p.slug: p for p in library.auto_profiles()}
    if isinstance(proposed, str) and proposed in allowed:
        return allowed[proposed]
    if isinstance(proposed, str) and library.profile(proposed):
        notes.append(
            f"Profile '{proposed}' comes from this project, so it is only used when you name "
            f"it (--permission {proposed}); using '{skill.default_permission}'."
        )
    profile = library.profile(skill.default_permission) or library.profile("read-only")
    if profile is None:
        raise RouteError("no permission profile is available")
    return profile


# ── building the plan ────────────────────────────────────────────────────────


def _plan_from_answer(
    task: str,
    skill: Skill,
    profile: PermissionProfile,
    answer: dict[str, Any],
    notes: list[str],
) -> SwarmPlan:
    agents = _agents(skill, task, answer.get("agents"))
    ids = [a.id for a in agents]
    lead = str(answer.get("lead") or "").lower() if answer else ""
    if lead not in ids:
        lead = skill.lead if skill.lead in ids else ids[-1]
    flow_text = answer.get("flow") if answer else None
    stages = (
        [list(s) for s in parse_flow(str(flow_text), ids)]
        if flow_text
        else _default_stages(skill, agents)
    )
    stages = _lead_last(stages, lead, notes)

    output = str(answer.get("output") or skill.output).lower() if answer else skill.output
    if output not in OUTPUTS:
        output = skill.output
    output = _downgrade(output, profile, notes)

    for agent in agents:
        agent.can_write = agent.role in skill.writers and profile.write == "workspace"
    stages = _split_writers(stages, {a.id: a for a in agents}, notes)
    for index, stage in enumerate(stages):
        for agent_id in stage:
            next(a for a in agents if a.id == agent_id).stage = index
    return SwarmPlan(
        task=task,
        skill=skill.slug,
        permission=profile.slug,
        output=output,
        lead=lead,
        agents=agents,
        stages=stages,
        notes=notes,
    )


def _agents(skill: Skill, task: str, proposed: Any) -> list[AgentSpec]:
    if not proposed:
        return [AgentSpec(id=role, role=role, task=task) for role in skill.team]
    if not isinstance(proposed, list):
        raise RouteError("'agents' must be a list")
    roles = [str(item.get("role", "")).lower() for item in proposed if isinstance(item, dict)]
    if len(roles) != len(proposed):
        raise RouteError("each agent must be an object")
    agents: list[AgentSpec] = []
    for item, role in zip(proposed, roles, strict=True):
        if role not in skill.team:
            raise RouteError(f"role '{role}' is not in the {skill.slug} team")
        agent_id = str(item.get("id") or "").strip().lower()
        if not agent_id:
            if roles.count(role) > 1:
                raise RouteError(f"role '{role}' is used twice, so each needs an id")
            agent_id = role
        if not _NAME_RE.match(agent_id):
            raise RouteError(f"id '{agent_id}' must be lowercase letters, digits and '-'")
        if any(a.id == agent_id for a in agents):
            raise RouteError(f"id '{agent_id}' is used twice")
        sub_task = str(item.get("task") or "").strip() or task
        agents.append(AgentSpec(id=agent_id, role=role, task=sub_task))
    if not agents:
        raise RouteError("the team is empty")
    if len(agents) > min(skill.max_agents, MAX_AGENTS):
        raise RouteError(f"{len(agents)} agents is more than this skill allows")
    return agents


def _default_stages(skill: Skill, agents: list[AgentSpec]) -> list[list[str]]:
    """Order agents by their role's stage in the skill's own flow."""
    position = {role: i for i, stage in enumerate(skill.flow) for role in stage}
    stages: dict[int, list[str]] = {}
    for agent in agents:
        stages.setdefault(position[agent.role], []).append(agent.id)
    return [stages[key] for key in sorted(stages)]


def _lead_last(stages: list[list[str]], lead: str, notes: list[str]) -> list[list[str]]:
    if stages[-1] == [lead]:
        return stages
    rest = [[a for a in stage if a != lead] for stage in stages]
    notes.append(f"Moved the lead '{lead}' to run last, on its own.")
    return [stage for stage in rest if stage] + [[lead]]


def _downgrade(output: str, profile: PermissionProfile, notes: list[str]) -> str:
    allowed = ALLOWED_OUTPUTS[profile.write]
    if output in allowed:
        return output
    if profile.write == "proposal":  # files -> one diff for approval
        notes.append("New files are proposed as one set of edits for your approval.")
        return "diff"
    notes.append(f"'{profile.slug}' cannot change files: the changes are shown, not applied.")
    return "text"


def _split_writers(
    stages: list[list[str]], agents: dict[str, AgentSpec], notes: list[str]
) -> list[list[str]]:
    """Agents that change files never run at the same time."""
    result: list[list[str]] = []
    for stage in stages:
        writers = [a for a in stage if agents[a].can_write]
        if len(writers) <= 1:
            result.append(stage)
            continue
        readers = [a for a in stage if not agents[a].can_write]
        result.append(readers + writers[:1])
        result.extend([w] for w in writers[1:])
        notes.append(f"{', '.join(writers)} all change files, so they run one after another.")
    return result
