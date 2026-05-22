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

    if provider:
        records = run_async(discovery.scan_provider(provider_id=provider))
    else:
        records = run_async(discovery.scan_all())

    table = Table(title="Discovered Models")
    table.add_column("Provider", style="cyan")
    table.add_column("Model", style="green")
    table.add_column("Specialization", style="magenta")
    table.add_column("Speed", style="blue")
    table.add_column("Context", style="yellow")
    table.add_column("Embedding", style="white")

    from velune.core.types.model import CapabilityLevel

    for record in records:
        # Dynamically infer highest specialization from capability profile
        capabilities_map = {
            "coding": record.capabilities.coding,
            "reasoning": record.capabilities.reasoning,
            "planning": record.capabilities.planning,
            "summarization": record.capabilities.summarization,
            "tool_use": record.capabilities.tool_use,
        }
        
        highest_cap = "general"
        highest_level = CapabilityLevel.NONE
        for cap_name, level in capabilities_map.items():
            if level > highest_level:
                highest_level = level
                highest_cap = cap_name
                
        specialization = highest_cap if highest_level > CapabilityLevel.NONE else "general"
        embedding_supported = "yes" if record.capabilities.embedding > CapabilityLevel.NONE else "no"

        table.add_row(
            record.provider_id,
            record.model_id,
            specialization,
            record.speed_tier,
            str(record.context_length),
            embedding_supported,
        )

    console.print(table)
    
    total = len(records)
    providers = set(r.provider_id for r in records)
    console.print(
        f"[dim]Discovered {total} model(s) across {len(providers)} provider(s).[/dim]"
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
        from velune.core.types.model import CapabilityLevel
        for record in registry.list_all():
            capabilities = []
            for cap_name in ["coding", "reasoning", "planning", "summarization", "tool_use", "long_context"]:
                level = getattr(record.capabilities, cap_name, None)
                if level and level > CapabilityLevel.NONE:
                    capabilities.append(f"{cap_name} ({level.name})")
            
            table.add_row(
                record.model_id,
                record.display_name,
                record.provider_id,
                ", ".join(capabilities) or "none",
            )

    console.print(table)


@models_cmd.command("assign")
def models_assign(
    ctx: typer.Context,
    role: str = typer.Argument(..., help="Agent role (planner, coder, reviewer, challenger, synthesizer)"),
    model_id: str = typer.Argument(..., help="Model ID to assign"),
) -> None:
    """Assign a model to an agent role."""
    cli_context = ctx.obj if isinstance(ctx.obj, CLIContext) else None
    orchestrator = cli_context.container.get("runtime.council_orchestrator") if cli_context else None

    if orchestrator is None:
        console.print("[red]Council orchestrator is unavailable.[/red]")
        raise typer.Exit(code=1)

    mapper = orchestrator.mapper
    try:
        from velune.models.specializations import CouncilRole
        council_role = CouncilRole(role.lower())
    except ValueError:
        console.print(f"[red]Invalid role '{role}'. Must be one of: planner, coder, reviewer, challenger, synthesizer[/red]")
        raise typer.Exit(code=1)

    # Check if model exists
    registry = cli_context.container.get("runtime.model_registry") if cli_context else None
    if registry:
        descriptor = registry.get(model_id)
        if not descriptor:
            console.print(f"[yellow]Warning: Model '{model_id}' is not currently registered/discovered.[/yellow]")

    mapper.overrides[council_role] = model_id
    console.print(f"[green]Successfully assigned role '{council_role.value}' to model '{model_id}' for the current runtime context.[/green]")
