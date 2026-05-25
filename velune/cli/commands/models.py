"""Models command - velune models scan/list/assign."""

from __future__ import annotations

from typing import Any

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
    discovery = cli_context.container.get("runtime.model_discovery") if cli_context else None

    if discovery is None:
        console.print("[red]Model discovery service is unavailable.[/red]")
        raise typer.Exit(code=1)

    from velune.core.event_loop import submit
    records = submit(_models_scan_async(discovery, provider))

    table = Table(title="Discovered Models")
    table.add_column("Provider", style="cyan")
    table.add_column("Model", style="green")
    table.add_column("Specialization", style="magenta")
    table.add_column("Speed", style="blue")
    table.add_column("Context", style="yellow")
    table.add_column("Embedding", style="white")
    table.add_column("Status", style="bold")

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

        validated = record.metadata.get("validated")
        if validated is None:
            status = "[dim]cached[/dim]"
        elif validated:
            status = "[green]●[/green]"
        else:
            status = "[red]✗ offline[/red]"

        table.add_row(
            record.provider_id,
            record.model_id,
            specialization,
            record.speed_tier,
            str(record.context_length),
            embedding_supported,
            status,
        )

    console.print(table)

    total = len(records)
    providers = set(r.provider_id for r in records)
    console.print(
        f"[dim]Discovered {total} model(s) across {len(providers)} provider(s).[/dim]"
    )


async def _models_scan_async(discovery: Any, provider: str | None) -> Any:
    if provider:
        return await discovery.scan_provider(provider_id=provider)
    else:
        return await discovery.scan_all()


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

    records = []
    if registry is None:
        table.add_row("<uninitialized>", "Velune", "system", "bootstrap only")
    else:
        from velune.core.types.model import CapabilityLevel
        records = registry.list_all()
        for record in records:
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

    # Get GPU info and show VRAM details
    gpu_info = None
    if cli_context:
        try:
            gpu_info = cli_context.container.get("runtime.gpu_info")
        except Exception:
            pass

    if gpu_info and gpu_info.get("has_gpu"):
        free_gb = gpu_info.get("vram_free_gb")
        if free_gb is not None:
            console.print(f"[dim]Available VRAM: {free_gb:.1f}GB[/dim]")

            over_budget = [m for m in records if
                           m.vram_required_gb and m.vram_required_gb > free_gb]
            if over_budget:
                console.print(f"[yellow]⚠ {len(over_budget)} models exceed available VRAM[/yellow]")


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


@models_cmd.command("benchmark")
def models_benchmark(
    ctx: typer.Context,
    model_id: str = typer.Argument(None, help="Specific model ID to benchmark. If omitted, benchmarks all registered models."),
) -> None:
    """Run capability probes on a specific model or all registered models."""
    cli_context = ctx.obj if isinstance(ctx.obj, CLIContext) else None
    if not cli_context:
        console.print("[red]CLI context is unavailable.[/red]")
        raise typer.Exit(code=1)

    registry = cli_context.container.get("runtime.model_registry")
    provider_registry = cli_context.container.get("runtime.provider_registry")

    if registry is None or provider_registry is None:
        console.print("[red]Model registry or provider registry is unavailable.[/red]")
        raise typer.Exit(code=1)

    # Get the models to benchmark
    models_to_probe = []
    if model_id:
        model = registry.get(model_id)
        if not model:
            console.print(f"[red]Model '{model_id}' is not registered.[/red]")
            raise typer.Exit(code=1)
        models_to_probe.append(model)
    else:
        models_to_probe = registry.list_all()

    if not models_to_probe:
        console.print("[yellow]No models registered for benchmarking.[/yellow]")
        return

    from velune.core.event_loop import submit
    submit(_models_benchmark_async(cli_context, registry, provider_registry, models_to_probe))


async def _models_benchmark_async(
    cli_context: CLIContext,
    registry: Any,
    provider_registry: Any,
    models_to_probe: list[Any],
) -> None:
    from pathlib import Path

    from velune.models.probes import ModelProber
    from velune.models.profile_cache import ModelProfileCache

    profile_cache = ModelProfileCache(Path(".velune") / "model_profiles.json")

    table = Table(title="Model Empirical Benchmark Results")
    table.add_column("Model ID", style="cyan")
    table.add_column("Provider", style="magenta")
    table.add_column("Coding Score (Lat)", style="green")
    table.add_column("Reasoning Score (Lat)", style="blue")
    table.add_column("Instruction Score (Lat)", style="yellow")
    table.add_column("Source", style="white")

    for model in models_to_probe:
        provider = provider_registry.get(model.provider_id)
        if not provider:
            console.print(f"[yellow]Skipping model '{model.model_id}': Provider '{model.provider_id}' is not active or available.[/yellow]")
            continue

        console.print(f"Benchmarking model [bold cyan]{model.model_id}[/bold cyan] via [bold magenta]{model.provider_id}[/bold magenta]...")

        prober = ModelProber(provider, model.model_id)
        results = await prober.run_all_probes()

        # Save to cache
        profile_cache.set(model.model_id, model.provider_id, results)
        # Apply to registry in-memory
        registry._apply_probe_results(model, results)

        coding = results["coding"]
        reasoning = results["reasoning"]
        instruction = results["instruction"]

        def format_result(res) -> str:
            if res.latency_ms < 0:
                return "[red]Failed[/red]"
            color = "green" if res.passed else "yellow"
            return f"[{color}]{res.score:.2f}[/{color}] ({res.latency_ms:.0f}ms)"

        table.add_row(
            model.model_id,
            model.provider_id,
            format_result(coding),
            format_result(reasoning),
            format_result(instruction),
            "empirical (forced)",
        )

    console.print(table)
