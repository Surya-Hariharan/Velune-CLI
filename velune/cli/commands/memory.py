"""Memory command - velune memory inspect/clear/export."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from velune.cli.context import CLIContext

console = Console()

memory_cmd = typer.Typer(help="Memory management commands")


@memory_cmd.command("inspect")
def memory_inspect(
    memory_type: str = typer.Option("all", "--type", "-t", help="Memory type (working, episodic, semantic, procedural, graph, all)"),
    limit: int = typer.Option(10, "--limit", "-l", help="Number of records to show"),
) -> None:
    """Inspect memory contents."""
    console.print(f"[yellow]Inspecting {memory_type} memory (limit: {limit})[/yellow]")
    console.print("[yellow]Memory inspection not yet fully implemented.[/yellow]")
    
    table = Table(title=f"Memory Records ({memory_type})")
    table.add_column("ID", style="cyan")
    table.add_column("Type", style="green")
    table.add_column("Content Preview", style="magenta")
    table.add_column("Importance", style="blue")
    
    console.print(table)


@memory_cmd.command("stats")
def memory_stats(ctx: typer.Context) -> None:
    """Show memory subsystem statistics and configured policy."""

    cli_context = ctx.obj if isinstance(ctx.obj, CLIContext) else None
    memory_config = cli_context.config.memory if cli_context else None

    table = Table(title="Memory Statistics")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    if memory_config is None:
        table.add_row("status", "bootstrap-only")
    else:
        table.add_row("working_memory_ttl", str(memory_config.working_memory_ttl))
        table.add_row("episodic_retention_days", str(memory_config.episodic_retention_days))
        table.add_row("semantic_threshold", str(memory_config.semantic_threshold))
        table.add_row("graph_enabled", str(memory_config.graph_enabled))
        table.add_row("workspace", str(cli_context.workspace))

    console.print(table)


@memory_cmd.command("clear")
def memory_clear(
    memory_type: str = typer.Argument(..., help="Memory type to clear"),
    confirm: bool = typer.Option(False, "--confirm", "-y", help="Skip confirmation"),
) -> None:
    """Clear memory of a specific type."""
    if not confirm:
        typer.confirm(f"Are you sure you want to clear {memory_type} memory?", abort=True)
    
    console.print(f"[yellow]Clearing {memory_type} memory[/yellow]")
    console.print("[yellow]Memory clearing not yet implemented.[/yellow]")


@memory_cmd.command("export")
def memory_export(
    output: Path = typer.Argument(..., help="Output file path"),
    memory_type: str = typer.Option("all", "--type", "-t", help="Memory type to export"),
) -> None:
    """Export memory to a file."""
    console.print(f"[yellow]Exporting {memory_type} memory to {output}[/yellow]")
    console.print("[yellow]Memory export not yet implemented.[/yellow]")
