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
    probe: bool = typer.Option(False, "--probe", help="Run empirical capability probes synchronously and cache results"),
) -> None:
    """Scan for available models."""
    cli_context = ctx.obj if isinstance(ctx.obj, CLIContext) else None
    if cli_context is None:
        if ctx.obj and getattr(ctx.obj, "json_mode", False):
            import json
            print(json.dumps({"error": "Model discovery service is unavailable"}))
        else:
            console.print("[red]Model discovery service is unavailable.[/red]")
        raise typer.Exit(code=1)

    from velune.core.event_loop import submit
    records = submit(_models_scan_async(cli_context, provider, probe))

    from velune.core.types.model import CapabilityLevel

    if cli_context.json_mode:
        import json
        out = []
        for record in records:
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
            embedding_supported = record.capabilities.embedding > CapabilityLevel.NONE
            validated = record.metadata.get("validated")
            status = "cached" if validated is None else ("online" if validated else "offline")

            out.append({
                "provider_id": record.provider_id,
                "model_id": record.model_id,
                "specialization": specialization,
                "speed_tier": record.speed_tier,
                "context_length": record.context_length,
                "embedding_supported": embedding_supported,
                "status": status,
            })
        print(json.dumps(out))
        return

    table = Table(title="Discovered Models")
    table.add_column("Provider", style="cyan")
    table.add_column("Model", style="green")
    table.add_column("Specialization", style="magenta")
    table.add_column("Speed", style="blue")
    table.add_column("Context", style="yellow")
    table.add_column("Embedding", style="white")
    table.add_column("Status", style="bold")

    for record in records:
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


async def _models_scan_async(
    cli_context: CLIContext,
    provider_id: str | None,
    probe: bool
) -> Any:
    container = cli_context.container
    lifecycle = container.get("runtime.lifecycle")
    discovery = container.get("runtime.model_discovery")
    provider_registry = container.get("runtime.provider_registry")

    if probe:
        await lifecycle.startup()

    try:
        if provider_id:
            records = await discovery.scan_provider(provider_id=provider_id)
        else:
            records = await discovery.scan_all()

        if probe:
            from pathlib import Path

            from velune.models.probes import FastProbe, ModelProber
            from velune.models.profile_cache import ModelProfileCache

            profile_cache = ModelProfileCache(Path(".velune") / "model_profiles.json")
            fast_probe = FastProbe()

            if not cli_context.json_mode:
                console.print("[bold cyan]⠋[/bold cyan] Probing discovered models synchronously...")

            probe_tasks = []
            valid_records = []

            for record in records:
                provider = provider_registry.get(record.provider_id)
                if provider:
                    valid_records.append(record)
                    probe_tasks.append(fast_probe.ping(provider, record.model_id))

            if valid_records:
                import asyncio
                responsiveness = await asyncio.gather(*probe_tasks, return_exceptions=True)

                empirical_probe_tasks = []
                probing_models = []

                for record, is_responsive in zip(valid_records, responsiveness):
                    if is_responsive is True:
                        provider = provider_registry.get(record.provider_id)
                        prober = ModelProber(provider, record.model_id)
                        probing_models.append((record, prober))
                        empirical_probe_tasks.append(prober.run_all_probes())
                        record.metadata["validated"] = True
                    else:
                        record.metadata["validated"] = False

                if empirical_probe_tasks:
                    if not cli_context.json_mode:
                        console.print(f"[bold magenta]⚡ Running empirical capability probes for {len(empirical_probe_tasks)} active model(s)...[/bold magenta]")
                    results = await asyncio.gather(*empirical_probe_tasks, return_exceptions=True)

                    for (record, prober), result in zip(probing_models, results):
                        if isinstance(result, Exception):
                            if not cli_context.json_mode:
                                console.print(f"[red]✗[/red] Probe failed for {record.model_id}: {result}")
                            continue

                        profile_cache.set(record.model_id, record.provider_id, result)

                        registry = container.get("runtime.model_registry")
                        if registry:
                            registry._apply_probe_results(record, result)
                            registry.register(record)

            if not cli_context.json_mode:
                console.print("[bold green]✓[/bold green] Empirical benchmarks completed and cached.")

        return records
    finally:
        if probe:
            await lifecycle.shutdown()


@models_cmd.command("list")
def models_list(ctx: typer.Context) -> None:
    """List registered models."""
    cli_context = ctx.obj if isinstance(ctx.obj, CLIContext) else None
    registry = cli_context.container.get("runtime.model_registry") if cli_context else None

    if cli_context and cli_context.json_mode:
        import json
        out = []
        if registry is not None:
            from velune.core.types.model import CapabilityLevel
            records = registry.list_all()
            for record in records:
                capabilities = []
                for cap_name in ["coding", "reasoning", "planning", "summarization", "tool_use", "long_context"]:
                    level = getattr(record.capabilities, cap_name, None)
                    if level and level > CapabilityLevel.NONE:
                        capabilities.append(cap_name)
                out.append({
                    "model_id": record.model_id,
                    "display_name": record.display_name,
                    "provider_id": record.provider_id,
                    "capabilities": capabilities,
                })
        print(json.dumps(out))
        return

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
        if cli_context and cli_context.json_mode:
            import json
            print(json.dumps({"error": "Council orchestrator is unavailable"}))
        else:
            console.print("[red]Council orchestrator is unavailable.[/red]")
        raise typer.Exit(code=1)

    mapper = orchestrator.mapper
    try:
        from velune.models.specializations import CouncilRole
        council_role = CouncilRole(role.lower())
    except ValueError:
        if cli_context and cli_context.json_mode:
            import json
            print(json.dumps({"error": f"Invalid role '{role}'"}))
        else:
            console.print(f"[red]Invalid role '{role}'. Must be one of: planner, coder, reviewer, challenger, synthesizer[/red]")
        raise typer.Exit(code=1)

    # Check if model exists
    registry = cli_context.container.get("runtime.model_registry") if cli_context else None
    if registry:
        descriptor = registry.get(model_id)
        if not descriptor and not (cli_context and cli_context.json_mode):
            console.print(f"[yellow]Warning: Model '{model_id}' is not currently registered/discovered.[/yellow]")

    mapper.overrides[council_role] = model_id
    if cli_context and cli_context.json_mode:
        import json
        print(json.dumps({"success": True, "role": council_role.value, "model_id": model_id}))
    else:
        console.print(f"[green]Successfully assigned role '{council_role.value}' to model '{model_id}' for the current runtime context.[/green]")


@models_cmd.command("benchmark")
def models_benchmark(
    ctx: typer.Context,
    model_id: str = typer.Argument(None, help="Specific model ID to benchmark. If omitted, benchmarks all registered models."),
) -> None:
    """Run capability probes on a specific model or all registered models."""
    cli_context = ctx.obj if isinstance(ctx.obj, CLIContext) else None
    if not cli_context:
        if ctx.obj and getattr(ctx.obj, "json_mode", False):
            import json
            print(json.dumps({"error": "CLI context is unavailable"}))
        else:
            console.print("[red]CLI context is unavailable.[/red]")
        raise typer.Exit(code=1)

    registry = cli_context.container.get("runtime.model_registry")
    provider_registry = cli_context.container.get("runtime.provider_registry")

    if registry is None or provider_registry is None:
        if cli_context.json_mode:
            import json
            print(json.dumps({"error": "Model registry or provider registry is unavailable"}))
        else:
            console.print("[red]Model registry or provider registry is unavailable.[/red]")
        raise typer.Exit(code=1)

    # Get the models to benchmark
    models_to_probe = []
    if model_id:
        model = registry.get(model_id)
        if not model:
            if cli_context.json_mode:
                import json
                print(json.dumps({"error": f"Model '{model_id}' is not registered"}))
            else:
                console.print(f"[red]Model '{model_id}' is not registered.[/red]")
            raise typer.Exit(code=1)
        models_to_probe.append(model)
    else:
        models_to_probe = registry.list_all()

    if not models_to_probe:
        if cli_context.json_mode:
            import json
            print(json.dumps([]))
        else:
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
    import json
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

    json_results = []

    for model in models_to_probe:
        provider = provider_registry.get(model.provider_id)
        if not provider:
            if not cli_context.json_mode:
                console.print(f"[yellow]Skipping model '{model.model_id}': Provider '{model.provider_id}' is not active or available.[/yellow]")
            continue

        if not cli_context.json_mode:
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

        if cli_context.json_mode:
            json_results.append({
                "model_id": model.model_id,
                "provider_id": model.provider_id,
                "results": {
                    "coding": {"score": coding.score, "latency_ms": coding.latency_ms, "passed": coding.passed},
                    "reasoning": {"score": reasoning.score, "latency_ms": reasoning.latency_ms, "passed": reasoning.passed},
                    "instruction": {"score": instruction.score, "latency_ms": instruction.latency_ms, "passed": instruction.passed}
                }
            })
        else:
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

    if cli_context.json_mode:
        print(json.dumps(json_results))
    else:
        console.print(table)
