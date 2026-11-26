"""``velune run <task>`` — run a task with the agent swarm from the command line."""

from __future__ import annotations

import asyncio
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from velune.cli.context import CLIContext

console = Console()
run_cmd = typer.Typer(help="Autonomous council run commands")


def run_command(
    ctx: typer.Context,
    task: str = typer.Argument(..., help="Natural-language task to execute"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        "-d",
        help="Plan and answer, but do not write files or run commands",
    ),
    force: bool = typer.Option(
        False, "--force", "-f", help="Allow changes without asking (AUTO mode)"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip cost confirmation prompts (for scripting)"
    ),
    skill: str | None = typer.Option(None, "--skill", help="Skill to use (see /skills)"),
    permission: str | None = typer.Option(
        None, "--permission", help="Permission profile to use (see /skills)"
    ),
) -> None:
    """Run a task: a team of agents plans it, and changes go through your approval."""

    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    # Propagate --yes into shared context so async helpers can read it
    cli_context.yes = yes or cli_context.yes

    from velune.core.event_loop import submit

    submit(_run_command_async(cli_context, task, dry_run, force, skill, permission))


def run_mode(dry_run: bool, force: bool):
    """--dry-run → PLAN (nothing changes), --force → AUTO, otherwise MANUAL (ask)."""
    from velune.permissions import ExecutionMode

    if dry_run:
        return ExecutionMode.PLAN
    return ExecutionMode.AUTO if force else ExecutionMode.MANUAL


async def terminal_ask(tool_name: str, decision: Any) -> Any:
    """Approve one action at the terminal (no full-screen app is running here)."""
    from rich.prompt import Confirm

    from velune.permissions.gate import Approval

    targets = ", ".join(str(a.target) for a in getattr(decision, "actions", [])[:5])
    console.print(
        f"\n[bold yellow]{escape(tool_name)}[/bold yellow] wants to: "
        f"{escape(getattr(decision, 'reason', '') or 'make a change')}"
        + (f"\n[dim]{escape(targets)}[/dim]" if targets else "")
    )
    allowed = await asyncio.to_thread(Confirm.ask, "Allow?", default=False, console=console)
    return Approval.ALLOW_ONCE if allowed else Approval.DENY


async def _run_command_async(
    cli_context: CLIContext,
    task: str,
    dry_run: bool,
    force: bool,
    skill: str | None = None,
    permission: str | None = None,
) -> None:
    container = cli_context.container
    lifecycle = container.get("runtime.lifecycle")
    try:
        await lifecycle.startup()
        await _run_with_runtime(cli_context, task, dry_run, force, skill, permission)
    finally:
        await lifecycle.shutdown()


async def _run_with_runtime(
    cli_context: CLIContext,
    task: str,
    dry_run: bool,
    force: bool,
    skill: str | None,
    permission: str | None,
) -> None:
    import json

    from velune.cli.commands.ask import outcome_json
    from velune.cli.commands.preflight import run_preflight_check
    from velune.cli.model_prefs import load_active_model
    from velune.swarm.router import RouteError
    from velune.swarm.setup import run_headless

    container = cli_context.container
    quiet = cli_context.json_mode
    if not quiet:
        console.print("[bold cyan]Probing local and remote providers...[/bold cyan]")
    await container.get("runtime.model_registry").refresh()
    if not await run_preflight_check(container, None if quiet else console):
        message = "Preflight check failed. Ensure workspace is initialized and models are scanned."
        if quiet:
            print(json.dumps({"error": message}))
        return

    mode = run_mode(dry_run, force)
    if not quiet:
        console.print(
            Panel(
                Text(task),
                border_style="cyan",
                title=f"[bold cyan]Council run · {mode.value.lower()}[/bold cyan]",
            )
        )
        _maybe_confirm_cost(cli_context, task)

    try:
        outcome = await run_headless(
            container,
            task,
            mode=mode,
            skill=skill,
            permission=permission,
            ask=None if quiet else terminal_ask,
            preferred=load_active_model(),
        )
    except (RouteError, RuntimeError) as exc:
        if quiet:
            print(json.dumps({"error": str(exc)}))
        else:
            console.print(f"[bold red]{escape(str(exc))}[/bold red]")
        return

    if quiet:
        print(json.dumps(outcome_json(task, outcome)))
        return
    render_outcome(outcome)
    _print_run_next_steps(success=outcome.ok, run_id=outcome.run_id)


def render_outcome(outcome: Any) -> None:
    """The lead's answer, or why the run failed, plus how each agent finished."""
    from velune.cli.rendering import CustomMarkdown

    agents = Text()
    for result in outcome.results:
        agents.append(f"\n{result.agent_id} · {result.status}", style="dim")
        if result.error:
            agents.append(f": {result.error}", style="dim")
    if outcome.ok:
        from rich.console import Group

        console.print(
            Panel(
                Group(CustomMarkdown(outcome.content), agents),
                border_style="green",
                title=f"[bold green]{outcome.plan.skill} · {outcome.plan.permission}[/bold green]",
            )
        )
        if outcome.plan.output == "diff":
            console.print(
                "[dim]The edits above are proposed, not applied. Open [bold]velune[/bold] and use "
                "[bold]/council[/bold] to review and apply them.[/dim]"
            )
        return
    body = Text(outcome.error or "The council could not produce an answer.", style="bold red")
    body.append_text(agents)
    console.print(
        Panel(
            body,
            border_style="red" if outcome.status == "failed" else "yellow",
            title="[bold red]Council Failed[/bold red]"
            if outcome.status == "failed"
            else "[bold yellow]Council Stopped[/bold yellow]",
        )
    )


def _print_run_next_steps(*, success: bool, run_id: str) -> None:
    """Close the run with review steps (success) or recovery steps (failure)."""
    from velune.cli import guidance, ui

    outcome = "run_succeeded" if success else "run_rolled_back"
    steps = guidance.steps_for(outcome, run_id=run_id)
    if not steps:
        return
    if success:
        console.print(ui.next_steps("Run complete", "Review what changed with git diff.", steps))
    else:
        console.print(
            ui.next_steps(
                "Run failed",
                "Changes you approved before it stopped are kept; /rewind is not available here.",
                steps,
                kind="warning",
            )
        )


def _maybe_confirm_cost(cli_context: CLIContext, task: str) -> None:
    """Estimate cost of the upcoming run and prompt for confirmation if above threshold.

    Raises SystemExit if the user declines.  Skipped when --yes is set.
    """
    from velune.telemetry.cost_estimator import CostEstimator

    # Heuristic: estimate from task text alone as lower-bound; council adds far more tokens.
    # We use a conservative 8× multiplier to account for repo context + multi-agent turns.
    task_messages = [{"role": "user", "content": task}]

    try:
        model_registry = cli_context.container.get("runtime.model_registry")
        models = model_registry.list_all() if model_registry else []
    except Exception:
        models = []

    # Pick the first non-local model as the representative for cost estimation
    cloud_model = next((m for m in models if not getattr(m, "is_local", False)), None)
    if cloud_model is None:
        return  # All local — no cost to warn about

    estimator = CostEstimator()
    base_tokens = estimator.estimate_tokens(task_messages, cloud_model)
    estimated_tokens = base_tokens * 8  # council overhead multiplier
    cost = estimator.estimate_cost(estimated_tokens, cloud_model)

    if cost is None:
        return

    threshold = 0.01
    try:
        threshold = cli_context.config.providers.cost_threshold_usd
    except Exception:
        pass

    if cost <= threshold:
        return  # Below threshold — proceed silently

    estimate_str = estimator.format_estimate(estimated_tokens, cost, cloud_model)
    console.print(f"\n[yellow]Estimated cost:[/yellow] {estimate_str}")

    if cli_context.yes:
        return

    answer = console.input("Proceed? [Y/n] ").strip().lower()
    if answer in ("n", "no"):
        console.print("[red]Aborted.[/red]")
        raise typer.Exit(0)
