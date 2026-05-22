"""Interactive ask command boundary — routes natural language questions to the Council."""

from __future__ import annotations

import asyncio
from typing import Optional
import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from velune.cli.context import CLIContext
from velune.cli.display.council_view import CouncilDisplayView
from velune.core.async_runtime import run_async
from velune.repository.schemas import RepositorySnapshot

console = Console()
ask_cmd = typer.Typer(help="Interactive prompt entry point")


@ask_cmd.callback(invoke_without_command=True)
def ask_command(
    ctx: typer.Context,
    prompt: Optional[str] = typer.Argument(None, help="Question or task to route through Velune"),
) -> None:
    """Deliberates with the Reasoning Council for conceptual answers and code reviews without execution."""

    if ctx.invoked_subcommand is not None:
        return

    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    if not prompt:
        # Prompt user interactively if no prompt argument is given
        prompt = typer.prompt("What would you like to ask Velune?")
        if not prompt:
            console.print("[yellow]Empty query. Exiting.[/yellow]")
            return

    # 1. Access services from DI container
    container = cli_context.container
    lifecycle = container.get("runtime.lifecycle")
    model_registry = container.get("runtime.model_registry")
    model_specialization = container.get("runtime.council_orchestrator").mapper
    repo_cognition = container.get("runtime.repository_cognition")
    orchestrator = container.get("runtime.council_orchestrator")

    # 2. Boot subsystems
    console.print("[bold cyan]⠋[/bold cyan] Bootstrapping Cognitive Operating System kernel...")
    run_async(lifecycle.startup())
    run_async(model_registry.refresh())

    display = CouncilDisplayView(console)
    display.render_header(prompt)

    # 3. Map role specializations
    roles = model_specialization.map_roles()
    display.render_role_assignments(roles)

    # 4. Ingest and Scan AST Snapshot
    snapshot: RepositorySnapshot = None
    with console.status("[bold magenta]⚡ Scanning codebase AST structure...[/bold magenta]") as status:
        snapshot = repo_cognition.index()
    
    formatted_snap = _format_snapshot_context(snapshot)

    # 5. deliberating debate loop
    council_res = None
    with console.status("[bold magenta]🧠 Deliberating Reasoning Council debate...[/bold magenta]") as status:
        council_res = run_async(orchestrator.execute_task(prompt, formatted_snap))

    arbitration = council_res["arbitration"]
    final_summary = council_res["final_summary"]

    # 6. Render reports
    display.render_step_header("Council Reviewer", "🔍")
    display.render_reviewer_report(council_res["reviewer_report"])

    display.render_step_header("Council Challenger", "⚡")
    display.render_challenger_report(council_res["challenger_report"])

    display.render_step_header("Arbitration Engine", "⚖️")
    display.render_arbitration_result(arbitration)

    display.render_step_header("Council Synthesizer", "🚀")
    display.render_synthesized_response(final_summary)

    # 7. Shutdown
    run_async(lifecycle.shutdown())


def _format_snapshot_context(snapshot: RepositorySnapshot) -> str:
    """Format snapshot metadata context for query prompt."""
    lines = []
    lines.append(f"Repository Root: {snapshot.root_path}")
    lines.append("\nCodebase Files:")
    for f in snapshot.files[:25]:  # limit to prompt sizing
        lines.append(f"  - {f.path} ({f.language.value})")
        if f.symbols:
            syms = [s.name for s in f.symbols[:3]]
            lines.append(f"    Symbols: {', '.join(syms)}")
    return "\n".join(lines)