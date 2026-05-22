"""Models command - velune models scan/list/assign."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from velune.cli.context import CLIContext

console = Console()

models_cmd = typer.Typer(help="Model management commands")


@models_cmd.command("scan")
def models_scan(
    ctx: typer.Context,
    provider: str = typer.Option(None, "--provider", "-p", help="Specific provider to scan"),
) -> None:
    """Scan for available models."""
    cli_context = ctx.obj if isinstance(ctx.obj, CLIContext) else None
    registry = cli_context.container.get("runtime.provider_registry") if cli_context else None

    console.print("[bold]Provider scan boundary[/bold]")
    console.print(f"Workspace: {cli_context.workspace if cli_context else 'current process'}")
    if provider:
        console.print(f"Requested provider filter: {provider}")

    if registry is None:
        console.print("[yellow]No provider registry is available yet.[/yellow]")
        return

    table = Table(title="Registered Providers")
    table.add_column("Name", style="cyan")
    table.add_column("Status", style="green")
    table.add_column("Notes", style="magenta")

    for provider_name in registry.list_providers():
        table.add_row(provider_name, "registered", "discovery and routing boundary")

    console.print(table)


@models_cmd.command("list")
def models_list(ctx: typer.Context) -> None:
    """List registered models."""
    cli_context = ctx.obj if isinstance(ctx.obj, CLIContext) else None

    table = Table(title="Registered Models")
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="green")
    table.add_column("Provider", style="magenta")
    table.add_column("Capabilities", style="blue")

    if cli_context is None:
        table.add_row("<uninitialized>", "Velune", "system", "bootstrap only")
    else:
        provider_registry = cli_context.container.get("runtime.provider_registry")
        for provider_name in provider_registry.list_providers():
            table.add_row(provider_name, provider_name.title(), provider_name, "discovery pending")

    console.print(table)


@models_cmd.command("assign")
def models_assign(
    role: str = typer.Argument(..., help="Agent role (planner, coder, reasoner, etc.)"),
    model_id: str = typer.Argument(..., help="Model ID to assign"),
) -> None:
    """Assign a model to an agent role."""
    console.print(f"[yellow]Assigning model {model_id} to role {role}[/yellow]")
    console.print("[yellow]Model assignment not yet implemented.[/yellow]")
