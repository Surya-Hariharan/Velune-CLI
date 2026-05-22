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
    """Deliberate with the multi-agent Reasoning Council and execute in the subprocess sandbox."""

    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    # 1. Access modern services from the DI container
    container = cli_context.container
    lifecycle = container.get("runtime.lifecycle")
    model_registry = container.get("runtime.model_registry")
    model_specialization = container.get("runtime.council_orchestrator").mapper
    repo_cognition = container.get("runtime.repository_cognition")
    retrieval = container.get("runtime.retrieval")
    orchestrator = container.get("runtime.council_orchestrator")
    executor = container.get("runtime.execution_executor")
    config = cli_context.config

    # 2. Boot up Cognitive OS Subsystems
    console.print("[bold cyan]⠋[/bold cyan] Bootstrapping Cognitive Operating System kernel...")
    run_async(lifecycle.startup())
    
    # 3. Refresh model catalog scan to assign specialized seats
    console.print("[bold cyan]⠋[/bold cyan] Probing system hardware and local/remote providers...")
    run_async(model_registry.refresh())
    
    display = CouncilDisplayView(console)
    display.render_header(task)

    # 4. Render Specialized Council Seats
    roles = model_specialization.map_roles()
    display.render_role_assignments(roles)

    # 5. Ingest and Scan Workspace AST + Git metadata
    snapshot: RepositorySnapshot = None
    with console.status("[bold magenta]⚡ Mapping repository AST & Git volatility metrics...[/bold magenta]") as status:
        snapshot = repo_cognition.index()
    
    # Format a dense representation of the snapshot for the council's context
    formatted_snap = _format_snapshot_context(snapshot)

    # 6. Execute Multi-Agent Council Deliberation Graph
    council_res = None
    with console.status("[bold magenta]🧠Deliberating Reasoning Council debate graph...[/bold magenta]") as status:
        council_res = run_async(orchestrator.execute_task(task, formatted_snap))

    # Parse council output data
    task_plan = council_res["task_plan"]
    coder_proposal = council_res["coder_proposal"]
    reviewer_report = council_res["reviewer_report"]
    challenger_report = council_res["challenger_report"]
    arbitration = council_res["arbitration"]
    final_summary = council_res["final_summary"]

    # 7. Render Debate Artifacts & Scores
    display.render_step_header("Council Planner", "📋")
    display.render_planner_dag(task_plan)

    display.render_step_header("Council Coder", "💻")
    display.render_code_proposal(coder_proposal)

    display.render_step_header("Council Reviewer", "🔍")
    display.render_reviewer_report(reviewer_report)

    display.render_step_header("Council Challenger", "⚡")
    display.render_challenger_report(challenger_report)

    display.render_step_header("Arbitration Engine", "⚖️")
    display.render_arbitration_result(arbitration)

    display.render_step_header("Council Synthesizer", "🚀")
    display.render_synthesized_response(final_summary)

    # 8. Deciding on Autonomous Execution Sandbox
    requires_human = arbitration.get("requires_human_review", False)
    overall_confidence = arbitration.get("overall_confidence", 0.0)

    if dry_run:
        console.print("\n[yellow]💡 Dry run active. Skipping sandbox modification execution.[/yellow]")
        run_async(lifecycle.shutdown())
        return

    # Check execution boundaries and confirm with user
    should_confirm = config.execution.require_confirmation or requires_human or (overall_confidence < 0.6)
    if should_confirm and not force:
        console.print()
        if requires_human:
            console.print("[bold red]⚠️  CRITICAL ALERT:[/bold red] The council arbitrator flagged this plan for manual intervention.")
        elif overall_confidence < 0.6:
            console.print("[bold yellow]⚠️  WARNING:[/bold yellow] Council confidence is low. Verification execution is highly recommended.")
        
        confirm = typer.confirm("Do you want to authorize autonomous sandbox execution of the plan?", default=True)
        if not confirm:
            console.print("[yellow]Execution aborted by user. Exiting gracefully.[/yellow]")
            run_async(lifecycle.shutdown())
            return

    # 9. Perform Sandbox Execution DAG Loop
    console.print("\n[bold magenta]⚙️  Autonomous execution sandbox active...[/bold magenta]")
    
    execution_result = None
    with console.status("[bold cyan]Executing plan steps, saving checkpoints, and verifying postconditions...[/bold cyan]") as status:
        execution_result = run_async(executor.execute_plan(task_plan, dry_run=False))

    # 10. Display Task Execution Results
    console.print()
    if execution_result.success:
        console.print(
            Panel(
                Text.assemble(
                    ("[bold green]✓ AUTONOMOUS SANDBOX EXECUTION FULLY COMPLETED[/bold green]\n"),
                    (f"Time Taken: [bold white]{execution_result.execution_time_ms:.1f}ms[/bold white]\n"),
                    (f"Plan Steps: [bold white]{execution_result.steps_completed} / {execution_result.steps_total} completed[/bold white]\n"),
                    ("[green]State postconditions successfully validated. No rollbacks triggered.[/green]")
                ),
                border_style="green",
                title="[bold green]Success Report[/bold green]"
            )
        )
    else:
        console.print(
            Panel(
                Text.assemble(
                    ("[bold red]✗ AUTONOMOUS EXECUTION BLOCKED & ROLLED BACK[/bold red]\n"),
                    (f"Execution Error: [bold red]{execution_result.error or 'Unexpected validation/compile failure'}[/bold red]\n"),
                    (f"Plan Steps: [bold white]{execution_result.steps_completed} / {execution_result.steps_total} completed[/bold white]\n"),
                    ("[yellow]State checkpointer successfully stashed snapshots and fully rolled back file edits to keep workspace clean.[/yellow]")
                ),
                border_style="red",
                title="[bold red]Rollback Execution Report[/bold red]"
            )
        )

    # 11. Graceful Shutdown
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
