"""Run command — velune run <task> to trigger Reasoning Council deliberation and sandbox execution."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from velune.cli.context import CLIContext
from velune.cognition.firewall import CognitiveFirewall
from velune.repository.schemas import RepositorySnapshot

console = Console()
run_cmd = typer.Typer(help="Autonomous council run commands")


def run_command(
    ctx: typer.Context,
    task: str = typer.Argument(..., help="Natural-language task to execute"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        "-d",
        help="Deliberate but do not write modifications or execute scripts",
    ),
    force: bool = typer.Option(
        False, "--force", "-f", help="Force execution without human confirm thresholds"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip cost confirmation prompts (for scripting)"
    ),
) -> None:
    """Deliberate with the stateful LangGraph Reasoning Council and execute in the secured sandbox."""

    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    # Propagate --yes into shared context so async helpers can read it
    cli_context.yes = yes or cli_context.yes

    from velune.core.event_loop import submit

    submit(_run_command_async(cli_context, task, dry_run, force))


async def _run_command_async(
    cli_context: CLIContext,
    task: str,
    dry_run: bool,
    force: bool,
) -> None:
    # 1. Access modern services from the DI container
    container = cli_context.container
    lifecycle = container.get("runtime.lifecycle")
    model_registry = container.get("runtime.model_registry")
    orchestration_engine = container.get("runtime.orchestration_engine")
    # 2. Boot up Cognitive OS Subsystems
    if not cli_context.json_mode:
        console.print("[bold cyan]⠋[/bold cyan] Bootstrapping Cognitive Operating System kernel...")
    await lifecycle.startup()

    # 3. Refresh model catalog scan to assign specialized seats
    if not cli_context.json_mode:
        console.print(
            "[bold cyan]⠋[/bold cyan] Probing system hardware and local/remote providers..."
        )
    await model_registry.refresh()

    # Onboarding preflight check gate
    from velune.cli.commands.preflight import run_preflight_check

    if not await run_preflight_check(container, console if not cli_context.json_mode else None):
        if cli_context.json_mode:
            import json

            print(
                json.dumps(
                    {
                        "error": "Preflight check failed. Ensure workspace is initialized and models are scanned."
                    }
                )
            )
        await lifecycle.shutdown()
        return

    if not cli_context.json_mode:
        console.print()
        console.print(
            Panel(
                f"[bold green]Task Auth:[/bold green] {task}",
                border_style="cyan",
                title="[bold cyan]Velune Stateful Execution Pipeline[/bold cyan]",
            )
        )
        # 4. Stream Multi-Agent Council Deliberation & Execution Graph
        console.print(
            "[bold magenta]🧠 Streaming LangGraph stateful execution & checkpoint pipeline...[/bold magenta]\n"
        )

    # --- Pre-operation cost estimation gate ---
    if not cli_context.json_mode:
        _maybe_confirm_cost(cli_context, task)

    from velune.orchestration.schemas import ExecutionStatus

    async def stream_runner():
        milestones = []
        async for milestone in orchestration_engine.stream(task):
            if not cli_context.json_mode:
                console.print(f"  [bold cyan]•[/bold cyan] {milestone}")
            milestones.append(milestone)

        # Parse run_id from milestones (format: "[run_id] milestone_name")
        run_id = None
        for m in milestones:
            if hasattr(m, "run_id"):
                run_id = m.run_id
                break
            elif isinstance(m, str) and m.startswith("[") and "]" in m:
                run_id = m.split("]")[0][1:]
                break
        return orchestration_engine.get_state(run_id) if run_id else None

    state = await stream_runner()

    # 5. Display Task Execution Results
    if not cli_context.json_mode:
        console.print()
    if state is None:
        if cli_context.json_mode:
            import json

            print(json.dumps({"error": "Pipeline failed to initialize state."}))
        else:
            console.print("[bold red]✗ Pipeline failed to initialize state.[/bold red]")
        await lifecycle.shutdown()
        return

    success = state.status == ExecutionStatus.COMPLETED
    attempts_count = len(state.attempts)
    plan_steps = len(state.task_plan.steps) if state.task_plan else 0
    checkpoints_count = len(state.checkpoints)

    if cli_context.json_mode:
        import json

        print(
            json.dumps(
                {
                    "success": success,
                    "run_id": state.run_id,
                    "plan_steps": plan_steps,
                    "retry_attempts": attempts_count,
                    "checkpoints_saved": checkpoints_count,
                    "output": state.output or "Execution completed successfully.",
                    "error": state.error,
                    "validation_issues": state.validation_issues or [],
                }
            )
        )
    else:
        if success:
            console.print(
                Panel(
                    Text.assemble(
                        ("[bold green]✓ STATEFUL AUTONOMOUS EXECUTION COMPLETED[/bold green]\n\n"),
                        (f"Run ID: [bold white]{state.run_id}[/bold white]\n"),
                        (f"Plan Steps: [bold white]{plan_steps}[/bold white] steps processed\n"),
                        (f"Retry Attempts: [bold white]{attempts_count}[/bold white]\n"),
                        (f"Checkpoints Saved: [bold white]{checkpoints_count}[/bold white]\n\n"),
                        ("[bold green]Synthesized Output:[/bold green]\n"),
                        (state.output or "Execution completed successfully."),
                    ),
                    border_style="green",
                    title="[bold green]Success Report[/bold green]",
                )
            )
        else:
            console.print(
                Panel(
                    Text.assemble(
                        ("[bold red]✗ AUTONOMOUS PIPELINE BLOCKED & ROLLED BACK[/bold red]\n\n"),
                        (f"Run ID: [bold white]{state.run_id}[/bold white]\n"),
                        (
                            f"Failure Reason: [bold red]{state.error or 'Validation/Execution mismatch'}[/bold red]\n"
                        ),
                        (f"Retry Attempts: [bold white]{attempts_count}[/bold white]\n"),
                        (
                            f"Validation Issues: [bold yellow]{', '.join(state.validation_issues) if state.validation_issues else 'None'}[/bold yellow]\n\n"
                        ),
                        (
                            "[yellow]State checkpointer stashed checkpoints, and Git workspace states have been preserved/rolled back.[/yellow]"
                        ),
                    ),
                    border_style="red",
                    title="[bold red]Rollback Execution Report[/bold red]",
                )
            )

    # 6. Graceful Shutdown
    await lifecycle.shutdown()


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


def _format_snapshot_context_safe(snapshot: RepositorySnapshot, firewall: CognitiveFirewall) -> str:
    """Format the RepositorySnapshot securely as a text summary context for the planner/coder agents."""
    lines = [f"Repository Root: {snapshot.root_path}"]
    lines.append("Codebase Files:")
    for f in snapshot.files[:25]:
        # Only expose path and language — no raw symbol names or content
        risk_marker = " [⚠ injection-risk]" if f.metadata.get("injection_risk") else ""
        lines.append(f"  - {f.path} ({f.language.value}){risk_marker}")
        # Symbol names are safe to expose (identifiers, not content)
        if f.symbols:
            safe_syms = [s.name for s in f.symbols[:3] if s.name.isidentifier()]
            if safe_syms:
                lines.append(f"    Symbols: {', '.join(safe_syms)}")
    return "\n".join(lines)
