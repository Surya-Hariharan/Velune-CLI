"""What ``/council`` prints while a swarm runs: the plan, each stage, each agent.

Everything is printed once, in order. There is no live region: the console is
the full-screen app's transcript, so a ``Live`` view would fight the composer.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from velune.cli import design
from velune.swarm.types import AgentResult, SwarmPlan

#: Argument shown after a tool's name, first match wins.
_TARGET_KEYS = ("file_path", "path", "pattern", "query", "command", "name", "agent_id")


def tool_label(name: str, arguments: Any) -> str:
    args = arguments if isinstance(arguments, dict) else {}
    target = next((str(args[k]) for k in _TARGET_KEYS if args.get(k)), "")
    return f"{name} {target[:70]}".strip()


def plan_table(plan: SwarmPlan) -> Table:
    table = Table(
        title=f"{plan.skill} · {plan.permission} · {plan.output}",
        title_justify="left",
        box=None,
        header_style=f"bold {design.ACCENT}",
        padding=(0, 1),
    )
    models = any(agent.model for agent in plan.agents)  # some role has its own model
    columns = ("Stage", "Agent", "Model", "Task") if models else ("Stage", "Agent", "Task")
    for column in columns:
        table.add_column(column, overflow="fold")
    for index, stage in enumerate(plan.stages, start=1):
        for agent_id in stage:
            agent = plan.agent(agent_id)
            name = agent_id if agent.role == agent_id else f"{agent_id} ({agent.role})"
            if agent_id == plan.lead:
                name += " · lead"
            if agent.can_write:
                name += " · writes"
            model = [escape(agent.model or "run model")] if models else []
            table.add_row(str(index), name, *model, escape(agent.task))
    return table


def outcome_lines(outcome: Any) -> list[str]:
    """What a run left behind, as plain lines: changed files, saved diagram, warnings."""
    lines: list[str] = []
    if outcome.files_changed:
        shown = ", ".join(outcome.files_changed[:10])
        more = len(outcome.files_changed) - 10
        if more > 0:
            shown += f" (+{more} more)"
        lines.append(f"Changed {len(outcome.files_changed)} file(s): {shown}")
    if outcome.diagram_path is not None:
        lines.append(f"Diagram saved: {outcome.diagram_path}")
    lines.extend(outcome.warnings)
    return lines


class ReplSwarmUI:
    """The ``SwarmUI`` for the REPL."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self._stages = 0

    def show_plan(self, plan: SwarmPlan) -> None:
        self._stages = len(plan.stages)
        self.console.print()
        self.console.print(plan_table(plan))
        for note in plan.notes:
            self.console.print(f"[dim]· {escape(note)}[/dim]")

    def stage_start(self, index: int, agent_ids: list[str], parallel: bool) -> None:
        how = " (parallel)" if parallel else ""
        self.console.print(
            f"[bold {design.ACCENT}]Stage {index + 1}/{self._stages}[/bold {design.ACCENT}] "
            f"· {', '.join(agent_ids)}{how}"
        )

    def agent_event(self, agent_id: str, event: str, data: dict[str, Any]) -> None:
        if event == "tool_start":
            label = tool_label(data.get("name", ""), data.get("arguments"))
            self.console.print(f"[dim]  {agent_id} · {escape(label)}[/dim]")
        elif event == "tool_denied":
            self.console.print(
                f"[yellow]  {agent_id} · {escape(str(data.get('name', '')))} refused:[/yellow] "
                f"[dim]{escape(str(data.get('reason', '')))}[/dim]"
            )

    def agent_done(self, result: AgentResult) -> None:
        tools = f" · {len(result.tool_calls)} tools" if result.tool_calls else ""
        if result.ok:
            self.console.print(
                f"[green]  ✓ {result.agent_id}[/green] [dim]· {result.elapsed_s:.0f}s{tools}[/dim]"
            )
        else:
            self.console.print(
                f"[red]  ✗ {result.agent_id}[/red] [dim]· {result.status}: "
                f"{escape(result.error)}[/dim]"
            )


class JobSwarmUI:
    """The ``SwarmUI`` for ``/council --bg``: reports the stage on the job record."""

    def __init__(self, update: Any) -> None:
        self._update = update  # called with current_phase=...
        self._stages = 0

    def show_plan(self, plan: SwarmPlan) -> None:
        self._stages = len(plan.stages)
        self._update(current_phase=f"{plan.skill}: planned")

    def stage_start(self, index: int, agent_ids: list[str], parallel: bool) -> None:
        self._update(current_phase=f"stage {index + 1}/{self._stages}")

    def agent_event(self, agent_id: str, event: str, data: dict[str, Any]) -> None:
        return None

    def agent_done(self, result: AgentResult) -> None:
        return None
