"""One swarm agent: its own conversation, its own tools, one result.

An agent is a ``ToolLoopRunner`` given two messages: a system message built
from the skill (shared knowledge plus this role's section), the permission
profile's guidance and an output contract; and a user message with the agent's
own sub-task, the user's request, what earlier agents found and the repository
context. Nothing else is shared between agents.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from velune.core.types.inference import InferenceRequest
from velune.orchestration.tool_loop import Approver, ToolLoopResult, ToolLoopRunner
from velune.swarm.library import PermissionProfile, Skill
from velune.swarm.policy import SwarmPolicy
from velune.swarm.store import RunStore
from velune.swarm.types import AgentResult, AgentSpec, SwarmPlan
from velune.tools.base.registry import ToolRegistry
from velune.tools.base.tool import ToolCallContext

logger = logging.getLogger("velune.swarm.agent")

EDIT_FORMAT = """Return each change as a SEARCH/REPLACE block:

path/to/file.py
<<<<<<< SEARCH
exact existing lines
=======
new lines
>>>>>>> REPLACE

SEARCH must copy the current file exactly (read it first). For a new file,
leave SEARCH empty."""

#: What the lead must hand back, per output type.
OUTPUT_CONTRACTS = {
    "text": (
        "Write the final answer for the user in Markdown. If code changes are "
        "needed, show them as code blocks; they are not applied automatically."
    ),
    "plan": (
        "Write the final plan in Markdown: overview, the design, numbered steps, how "
        "each step is checked, risks, open questions. Put diagrams in ```mermaid blocks."
    ),
    "diagram": ("Give the diagram as a ```mermaid code block, then a short explanation of it."),
    "diff": "Return the complete, final set of edits.\n\n"
    + EDIT_FORMAT
    + "\n\nAfter the blocks, add a short summary and anything you could not verify.",
    "files": (
        "Create or change the files with your file tools. Then list every file you "
        "created or changed, and anything you could not verify."
    ),
}

RULES = """Rules:
- Base every statement about the code on files you actually read; quote them with their path.
- Never invent files, functions, settings, APIs or results. Say what you could not verify.
- Stay within your own task; other agents handle the rest.
- Use swarm_note_write / swarm_note_read to share notes with the team, and
  swarm_result_read(agent_id) to read an earlier agent's full result."""

_WRITE_TOOLS = {
    "write_file",
    "edit_file",
    "create_file",
    "delete_file",
    "move_path",
    "create_directory",
    "delete_directory",
}


@dataclass
class AgentEnv:
    """Everything an agent runs with, built by the entry point (REPL, CLI, MCP)."""

    provider: Any  # ModelProvider, possibly budget-wrapped
    model_id: str
    registry: ToolRegistry | None = None
    mcp_registry: Any | None = None
    # Carries the session's permission gate. None (headless) means every state
    # change fails closed in authorize_and_execute.
    ctx: ToolCallContext | None = None
    base_approver: Approver | None = None
    context: str = ""  # repository context, already wrapped as untrusted content
    context_window: int = 32_768  # tokens
    on_event: Callable[[str, str, dict[str, Any]], None] | None = None  # (agent, event, data)
    tools: bool = True  # False for a model that cannot call tools: agents only answer


def build_messages(
    spec: AgentSpec,
    plan: SwarmPlan,
    skill: Skill,
    profile: PermissionProfile,
    policy: SwarmPolicy,
    store: RunStore,
    *,
    context: str = "",
    context_window: int = 32_768,
) -> list[dict[str, str]]:
    """The agent's system and user messages, kept within ~40% of the context window."""
    from velune.cognition.firewall import WORKSPACE_SANDBOX_NOTICE

    team = ", ".join(f"{a.id} ({a.role})" for a in plan.agents)
    access = profile.body
    if not policy.can_write and profile.write == "workspace":
        access += (
            "\n\nYou are read-only in this task: other agents change files. Return any "
            "change you propose as text."
        )
        if spec.role in skill.writers:
            access += "\n\n" + EDIT_FORMAT
    elif profile.write == "proposal" and spec.role in skill.writers:
        access += "\n\n" + EDIT_FORMAT
    parts = [
        f"You are the {spec.role} ('{spec.id}') in a team of agents working on one task: "
        f"{team}. Agents run in stages; you are in stage {spec.stage + 1} of "
        f"{len(plan.stages)}.",
        f"# Shared knowledge\n{skill.body}" if skill.body else "",
        f"# Your role: {spec.role}\n{skill.roles.get(spec.role, '')}",
        f"# What you may do ({profile.name})\n{access}",
    ]
    if spec.id == plan.lead:
        parts.append(
            "# Your output\nYou write the final result for the user.\n"
            + OUTPUT_CONTRACTS[plan.output]
        )
    parts += [RULES, WORKSPACE_SANDBOX_NOTICE]
    system = "\n\n".join(p for p in parts if p)

    budget = max(4_000, int(context_window * 0.4 * 4))  # characters (~4 per token)
    head = f"# Your task\n{spec.task}\n\n# The user's request\n{plan.task}"
    earlier = [_earlier(r, plan) for r in store.earlier_results(spec.stage)]
    room = budget - len(system) - len(head)
    sections = _fit(earlier, context, room)
    user = "\n\n".join(p for p in [head, *sections] if p)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _earlier(result: AgentResult, plan: SwarmPlan) -> str:
    role = plan.agent(result.agent_id).role
    if result.ok:
        return f"## {result.agent_id} ({role})\n{result.summary}"
    return f"## {result.agent_id} ({role}) failed: {result.status}: {result.error}"


def _fit(earlier: list[str], context: str, room: int) -> list[str]:
    """Trim repository context first, then earlier results; never the task."""
    results = "# Results so far\n" + "\n\n".join(earlier) if earlier else ""
    repo = f"# Repository context\n{context}" if context else ""
    if len(results) + len(repo) <= room:
        return [results, repo]
    repo_room = max(0, room - len(results))
    if repo_room >= 500:
        return [results, repo[:repo_room] + "\n… (repository context trimmed)"]
    return [results[: max(0, room)] + "\n… (trimmed)"] if results else []


async def run_agent(
    spec: AgentSpec,
    plan: SwarmPlan,
    skill: Skill,
    profile: PermissionProfile,
    policy: SwarmPolicy,
    store: RunStore,
    env: AgentEnv,
) -> AgentResult:
    """Run one agent to completion and record its result in the store."""
    agent_policy = policy.for_agent(can_write=spec.can_write)
    messages = build_messages(
        spec,
        plan,
        skill,
        profile,
        agent_policy,
        store,
        context=env.context,
        context_window=env.context_window,
    )

    def on_event(event: str, data: dict[str, Any]) -> None:
        if env.on_event is not None:
            env.on_event(spec.id, event, data)

    runner = ToolLoopRunner(
        env.provider,
        agent_policy.tool_registry(env.registry if env.tools else None),
        mcp_registry=env.mcp_registry if agent_policy.mcp and env.tools else None,
        approver=agent_policy.approver(env.base_approver),
        ctx=env.ctx,
        max_turns=spec.max_turns,
        on_event=on_event,
        dynamic_tools=store.scratch_tools(spec.id) if env.tools else None,
    )
    store.event("start", spec.id, role=spec.role, stage=spec.stage)
    started = time.perf_counter()
    try:
        loop_result = await runner.run(InferenceRequest(model_id=env.model_id, messages=messages))
    except asyncio.CancelledError:
        store.record(_failed(spec, "cancelled", "cancelled by the user", runner, started))
        raise
    except Exception as exc:
        status = "budget" if type(exc).__name__ == "BudgetExceeded" else "error"
        note_provider_failure(env.provider, exc)
        return store.record(_failed(spec, status, _describe(exc), runner, started))
    return store.record(_from_loop(spec, loop_result, started))


def note_provider_failure(provider: Any, exc: BaseException) -> None:
    """A rejected key (401) is marked invalid, so preflight and /connect can say so.

    A 403 (``ProviderPermissionError``) is not a bad key: the key may simply lack
    access to this model, so it is left alone.
    """
    from velune.core.errors.provider import ProviderAuthenticationError, ProviderPermissionError

    if not isinstance(exc, ProviderAuthenticationError) or isinstance(exc, ProviderPermissionError):
        return
    provider_id = getattr(provider, "provider_id", "")
    if not isinstance(provider_id, str) or not provider_id:
        return
    try:
        from velune.providers.keystore import mark_invalid

        mark_invalid(provider_id, reason=str(exc))
    except Exception:  # bookkeeping must never fail the run
        logger.debug("Could not mark %s invalid", provider_id, exc_info=True)


def _describe(exc: Exception) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _call_label(call: Any) -> str:
    args = call.arguments if isinstance(call.arguments, dict) else {}
    target = next(
        (str(args[k]) for k in ("file_path", "path", "pattern", "query") if k in args), ""
    )
    return f"{call.name}({target[:80]})"


def _touched(invocations: list[Any]) -> list[str]:
    files = []
    for inv in invocations:
        args = inv.call.arguments if isinstance(inv.call.arguments, dict) else {}
        target = args.get("file_path") or args.get("path")
        if inv.call.name in _WRITE_TOOLS and not inv.error and target and target not in files:
            files.append(str(target))
    return files


def _failed(spec: AgentSpec, status: str, error: str, runner, started: float) -> AgentResult:
    invocations = runner.executed_invocations
    return AgentResult(
        agent_id=spec.id,
        status=status,
        error=error,
        tool_calls=[_call_label(i.call) for i in invocations],
        files_touched=_touched(invocations),
        elapsed_s=time.perf_counter() - started,
    )


def _from_loop(spec: AgentSpec, result: ToolLoopResult, started: float) -> AgentResult:
    content = (result.content or "").strip()
    status, error = "ok", ""
    if result.stop_reason == "max_turns" and not content:
        status, error = "error", f"ran out of turns ({result.turns}) before answering"
    elif result.stop_reason == "repeated_failure":
        status, error = "error", content or "kept repeating a failing tool call"
    elif not content:
        status, error = "error", "the model returned no text"
    return AgentResult(
        agent_id=spec.id,
        status=status,
        content=content if status == "ok" else "",
        tool_calls=[_call_label(i.call) for i in result.invocations],
        files_touched=_touched(result.invocations),
        tokens=result.tokens_used,
        elapsed_s=time.perf_counter() - started,
        error=error,
    )
