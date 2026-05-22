"""Memory command - velune memory inspect/clear/export."""

import typer
from pathlib import Path
from rich.console import Console
from rich.table import Table

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
