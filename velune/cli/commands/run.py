"""Run command — velune run <task> to trigger Reasoning Council deliberation and sandbox execution."""

from __future__ import annotations

import asyncio
from pathlib import Path
import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from velune.cli.context import CLIContext
from velune.cli.display.council_view import CouncilDisplayView
from velune.core.async_runtime import run_async
from velune.repository.schemas import RepositorySnapshot

console = Console()
run_cmd = typer.Typer(help="Autonomous council run commands")


def run_command(
    ctx: typer.Context,
    task: str = typer.Argument(..., help="Natural-language task to execute"),
    dry_run: bool = typer.Option(False, "--dry-run", "-d", help="Deliberate but do not write modifications or execute scripts"),
    force: bool = typer.Option(False, "--force", "-f", help="Force execution without human confirm thresholds"),
) -> None:
    """Deliberate with the stateful LangGraph Reasoning Council and execute in the secured sandbox."""

    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    # 1. Access modern services from the DI container
    container = cli_context.container
    lifecycle = container.get("runtime.lifecycle")
    model_registry = container.get("runtime.model_registry")
    orchestration_engine = container.get("runtime.orchestration_engine")
    config = cli_context.config

    # 2. Boot up Cognitive OS Subsystems
    console.print("[bold cyan]⠋[/bold cyan] Bootstrapping Cognitive Operating System kernel...")
    run_async(lifecycle.startup())
    
    # 3. Refresh model catalog scan to assign specialized seats
    console.print("[bold cyan]⠋[/bold cyan] Probing system hardware and local/remote providers...")
    run_async(model_registry.refresh())
    
    console.print()
    console.print(Panel(f"[bold green]Task Auth:[/bold green] {task}", border_style="cyan", title="[bold cyan]Velune Stateful Execution Pipeline[/bold cyan]"))

    # 4. Stream Multi-Agent Council Deliberation & Execution Graph
    console.print("[bold magenta]🧠 Streaming LangGraph stateful execution & checkpoint pipeline...[/bold magenta]\n")
    
    from velune.orchestration.schemas import ExecutionStatus
    
    async def stream_runner():
        milestones = []
        async for milestone in orchestration_engine.stream(task):
            console.print(f"  [bold cyan]•[/bold cyan] {milestone}")
            milestones.append(milestone)
        
        # Parse run_id from milestones (format: "[run_id] milestone_name")
        run_id = None
        for m in milestones:
            if m.startswith("[") and "]" in m:
                run_id = m.split("]")[0][1:]
                break
        return orchestration_engine.get_state(run_id) if run_id else None

    state = run_async(stream_runner())

    # 5. Display Task Execution Results
    console.print()
    if state is None:
        console.print("[bold red]✗ Pipeline failed to initialize state.[/bold red]")
        run_async(lifecycle.shutdown())
        return

    success = state.status == ExecutionStatus.COMPLETED
    attempts_count = len(state.attempts)
    plan_steps = len(state.task_plan.steps) if state.task_plan else 0
    checkpoints_count = len(state.checkpoints)

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
                    (state.output or "Execution completed successfully.")
                ),
                border_style="green",
                title="[bold green]Success Report[/bold green]"
            )
        )
    else:
        console.print(
            Panel(
                Text.assemble(
                    ("[bold red]✗ AUTONOMOUS PIPELINE BLOCKED & ROLLED BACK[/bold red]\n\n"),
                    (f"Run ID: [bold white]{state.run_id}[/bold white]\n"),
                    (f"Failure Reason: [bold red]{state.error or 'Validation/Execution mismatch'}[/bold red]\n"),
                    (f"Retry Attempts: [bold white]{attempts_count}[/bold white]\n"),
                    (f"Validation Issues: [bold yellow]{', '.join(state.validation_issues) if state.validation_issues else 'None'}[/bold yellow]\n\n"),
                    ("[yellow]State checkpointer stashed checkpoints, and Git workspace states have been preserved/rolled back.[/yellow]")
                ),
                border_style="red",
                title="[bold red]Rollback Execution Report[/bold red]"
            )
        )

    # 6. Graceful Shutdown
    run_async(lifecycle.shutdown())


def _format_snapshot_context(snapshot: RepositorySnapshot) -> str:
    """Format the RepositorySnapshot as a clean text summary context for the planner/coder agents."""
    lines = []
    lines.append(f"Repository Root: {snapshot.root_path}")
    lines.append("\nCodebase Files:")
    for f in snapshot.files[:40]:  # limit to first 40 files for prompt sizing
        lines.append(f"  - {f.path} ({f.language.value}, {f.size_bytes} bytes)")
        if f.symbols:
            syms = [f"{s.kind.value} {s.name}" for s in f.symbols[:5]]
            lines.append(f"    Symbols (subset): {', '.join(syms)}")
    
    if len(snapshot.files) > 40:
        lines.append(f"  ... and {len(snapshot.files) - 40} other files.")

    summary = snapshot.summary
    if summary:
        git = summary.get("git", {})
        if git:
            lines.append(f"\nGit status:")
            lines.append(f"  Active Branch: {git.get('active_branch')}")
            lines.append(f"  Uncommitted changes count: {git.get('uncommitted_changes_count')}")
        arch = summary.get("architecture", {})
        if arch:
            lines.append(f"\nCodebase footprint:")
            lines.append(f"  Frameworks: {arch.get('frameworks_detected')}")
            lines.append(f"  Layer volumes: {arch.get('layers')}")

    return "\n".join(lines)
