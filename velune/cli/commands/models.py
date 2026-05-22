"""Models command - velune models scan/list/assign."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from velune.cli.context import CLIContext
from velune.core.async_runtime import run_async

console = Console()

models_cmd = typer.Typer(help="Model management commands")


@models_cmd.command("scan")
def models_scan(
    ctx: typer.Context,
    provider: str = typer.Option(None, "--provider", "-p", help="Specific provider to scan"),
) -> None:
    """Scan for available models."""
    cli_context = ctx.obj if isinstance(ctx.obj, CLIContext) else None
    discovery = cli_context.container.get("runtime.model_discovery") if cli_context else None

    if discovery is None:
        console.print("[red]Model discovery service is unavailable.[/red]")
        raise typer.Exit(code=1)

    records = run_async(discovery.discover(provider_id=provider))

    table = Table(title="Discovered Models")
    table.add_column("Provider", style="cyan")
    table.add_column("Model", style="green")
    table.add_column("Specialization", style="magenta")
    table.add_column("Speed", style="blue")
    table.add_column("Context", style="yellow")
    table.add_column("Embedding", style="white")

    for record in records:
        table.add_row(
            record.provider_id,
            record.model_id,
            record.classification.specialization.value,
            record.classification.speed_tier,
            str(record.classification.context_length),
            "yes" if record.classification.embedding_supported else "no",
        )

    console.print(table)
    summary = discovery.summary()
    console.print(
        f"[dim]Discovered {summary['total']} model(s) across {len(summary['providers'])} provider(s).[/dim]"
    )


@models_cmd.command("list")
def models_list(ctx: typer.Context) -> None:
    """List registered models."""
    cli_context = ctx.obj if isinstance(ctx.obj, CLIContext) else None
    registry = cli_context.container.get("runtime.model_registry") if cli_context else None

    table = Table(title="Registered Models")
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="green")
    table.add_column("Provider", style="magenta")
    table.add_column("Capabilities", style="blue")

    if registry is None:
        table.add_row("<uninitialized>", "Velune", "system", "bootstrap only")
    else:
        for record in registry.list():
            table.add_row(
                record.model_id,
                record.descriptor.display_name,
                record.provider_id,
                ", ".join(capability.value for capability in record.classification.capabilities.keys()) or "none",
            )

    console.print(table)


@models_cmd.command("assign")
def models_assign(
    role: str = typer.Argument(..., help="Agent role (planner, coder, reasoner, etc.)"),
    model_id: str = typer.Argument(..., help="Model ID to assign"),
) -> None:
    """Assign a model to an agent role."""
    console.print(f"[yellow]Assigning model {model_id} to role {role}[/yellow]")
    console.print("[yellow]Model assignment not yet implemented.[/yellow]")
