"""Models command - velune models scan/list/assign."""

from __future__ import annotations

from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from velune.cli.context import CLIContext

console = Console()

models_cmd = typer.Typer(help="Scan, list, and assign AI models.")


def _cap_badge(level) -> str:
    """Return a Rich markup checkmark/dash based on CapabilityLevel."""
    try:
        from velune.core.types.model import CapabilityLevel

        return "[green]✓[/green]" if level and level > CapabilityLevel.NONE else "[dim]—[/dim]"
    except Exception:
        return "[dim]—[/dim]"


def _health_badge(health: str) -> str:
    return {
        "healthy": "[green]●[/green]",
        "degraded": "[yellow]○[/yellow]",
        "offline": "[red]✗[/red]",
    }.get(health or "", "[dim]?[/dim]")


def _short_location(location: str | None, max_len: int = 28) -> str:
    if not location:
        return "[dim]—[/dim]"
    if location == "cloud":
        return "[blue]cloud[/blue]"
    return location if len(location) <= max_len else "…" + location[-(max_len - 1) :]


@models_cmd.command("scan")
def models_scan(
    ctx: typer.Context,
    provider: str = typer.Option(None, "--provider", "-p", help="Specific provider to scan"),
    probe: bool = typer.Option(
        False, "--probe", help="Run empirical capability probes synchronously and cache results"
    ),
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

            out.append(
                {
                    "provider_id": record.provider_id,
                    "model_id": record.model_id,
                    "specialization": specialization,
                    "speed_tier": record.speed_tier,
                    "context_length": record.context_length,
                    "embedding_supported": embedding_supported,
                    "vision": record.capabilities.vision > CapabilityLevel.NONE,
                    "tool_use": record.capabilities.tool_use > CapabilityLevel.NONE,
                    "reasoning": record.capabilities.reasoning > CapabilityLevel.NONE,
                    "health": getattr(record, "health", "unknown"),
                    "location": getattr(record, "location", None),
                    "status": status,
                }
            )
        print(json.dumps(out))
        return

    table = Table(title="Discovered Models")
    table.add_column("Provider", style="cyan", no_wrap=True)
    table.add_column("Model", style="green")
    table.add_column("Vision", justify="center")
    table.add_column("Tool", justify="center")
    table.add_column("Reason", justify="center")
    table.add_column("Embed", justify="center")
    table.add_column("Speed", style="blue")
    table.add_column("Context", style="yellow")
    table.add_column("Health", justify="center")
    table.add_column("Location", style="dim", max_width=28)

    for record in records:
        validated = record.metadata.get("validated")
        if validated is None:
            health_src = getattr(record, "health", "unknown")
        elif validated:
            health_src = "healthy"
        else:
            health_src = "offline"

        table.add_row(
            record.provider_id,
            record.model_id,
            _cap_badge(record.capabilities.vision),
            _cap_badge(record.capabilities.tool_use),
            _cap_badge(record.capabilities.reasoning),
            _cap_badge(record.capabilities.embedding),
            record.speed_tier,
            str(record.context_length),
            _health_badge(health_src),
            _short_location(getattr(record, "location", None)),
        )

    console.print(table)

    total = len(records)
    providers = {r.provider_id for r in records}
    console.print(f"[dim]Discovered {total} model(s) across {len(providers)} provider(s).[/dim]")


async def _models_scan_async(cli_context: CLIContext, provider_id: str | None, probe: bool) -> Any:
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
                console.print("[bold cyan]Probing discovered models...[/bold cyan]")

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

                for record, is_responsive in zip(valid_records, responsiveness, strict=False):
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
                        console.print(
                            f"[bold magenta]Running empirical capability probes for {len(empirical_probe_tasks)} active model(s)...[/bold magenta]"
                        )
                    results = await asyncio.gather(*empirical_probe_tasks, return_exceptions=True)

                    for (record, _), result in zip(probing_models, results, strict=False):
                        # gather(return_exceptions=True) can also surface BaseException
                        # subclasses (e.g. asyncio.CancelledError); treat any of them
                        # as a failed probe so they are never cached as valid results.
                        if isinstance(result, BaseException):
                            if not cli_context.json_mode:
                                console.print(
                                    f"[red]Probe failed for {record.model_id}: {result}[/red]"
                                )
                            continue

                        profile_cache.set(record.model_id, record.provider_id, result)

                        registry = container.get("runtime.model_registry")
                        if registry:
                            registry._apply_probe_results(record, result)
                            registry.register(record)

            if not cli_context.json_mode:
                console.print("[bold green]Empirical benchmarks completed and cached.[/bold green]")

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
                for cap_name in [
                    "coding",
                    "reasoning",
                    "planning",
                    "summarization",
                    "tool_use",
                    "long_context",
                ]:
                    level = getattr(record.capabilities, cap_name, None)
                    if level and level > CapabilityLevel.NONE:
                        capabilities.append(cap_name)
                out.append(
                    {
                        "model_id": record.model_id,
                        "display_name": record.display_name,
                        "provider_id": record.provider_id,
                        "capabilities": capabilities,
                    }
                )
        print(json.dumps(out))
        return

    table = Table(title="Registered Models")
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="green")
    table.add_column("Provider", style="magenta")
    table.add_column("Health", justify="center")
    table.add_column("Location", style="dim", max_width=28)
    table.add_column("Capabilities", style="blue")

    records = []
    if registry is None:
        table.add_row(
            "<uninitialized>", "Velune", "system", "[dim]?[/dim]", "[dim]—[/dim]", "bootstrap only"
        )
    else:
        from velune.core.types.model import CapabilityLevel

        records = registry.list_all()
        for record in records:
            capabilities = []
            for cap_name in [
                "coding",
                "reasoning",
                "planning",
                "summarization",
                "tool_use",
                "long_context",
            ]:
                level = getattr(record.capabilities, cap_name, None)
                if level and level > CapabilityLevel.NONE:
                    capabilities.append(f"{cap_name} ({level.name})")

            table.add_row(
                record.model_id,
                record.display_name,
                record.provider_id,
                _health_badge(getattr(record, "health", "unknown")),
                _short_location(getattr(record, "location", None)),
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

            over_budget = [
                m for m in records if m.vram_required_gb and m.vram_required_gb > free_gb
            ]
            if over_budget:
                console.print(f"[yellow]{len(over_budget)} models exceed available VRAM[/yellow]")


@models_cmd.command("assign")
def models_assign(
    ctx: typer.Context,
    role: str = typer.Argument(
        ..., help="Agent role (planner, coder, reviewer, challenger, synthesizer)"
    ),
    model_id: str = typer.Argument(..., help="Model ID to assign"),
) -> None:
    """Assign a model to an agent role."""
    cli_context = ctx.obj if isinstance(ctx.obj, CLIContext) else None
    orchestrator = (
        cli_context.container.get("runtime.council_orchestrator") if cli_context else None
    )

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
            console.print(
                f"[red]Invalid role '{role}'. Must be one of: planner, coder, reviewer, challenger, synthesizer[/red]"
            )
        raise typer.Exit(code=1)

    # Check if model exists and resolve provider_id for persistence
    registry = cli_context.container.get("runtime.model_registry") if cli_context else None
    provider_id = "unknown"
    if registry:
        descriptor = registry.get(model_id)
        if descriptor:
            provider_id = descriptor.provider_id
        elif not (cli_context and cli_context.json_mode):
            console.print(
                f"[yellow]Warning: Model '{model_id}' is not currently registered/discovered.[/yellow]"
            )

    # Update in-memory mapper for the current session
    mapper.overrides[council_role] = model_id

    # Persist to ~/.velune/council_roles.json (same file the REPL /councilmodel uses)
    from pathlib import Path as _Path

    from velune.orchestration.role_assignments import CouncilRoleMap

    _assignments_path = _Path.home() / ".velune" / "council_roles.json"
    try:
        _role_map = CouncilRoleMap.load(_assignments_path)
        _role_map.assign(council_role.value, model_id, provider_id)
        _role_map.save(_assignments_path)
    except Exception as _persist_exc:
        if not (cli_context and cli_context.json_mode):
            console.print(f"[yellow]Warning: could not persist assignment: {_persist_exc}[/yellow]")

    if cli_context and cli_context.json_mode:
        import json

        print(
            json.dumps(
                {
                    "success": True,
                    "role": council_role.value,
                    "model_id": model_id,
                    "provider_id": provider_id,
                }
            )
        )
    else:
        console.print(
            f"[green]Assigned role '{council_role.value}' → '{model_id}' (persisted to council_roles.json)[/green]"
        )


@models_cmd.command("benchmark")
def models_benchmark(
    ctx: typer.Context,
    model_id: str = typer.Argument(
        None, help="Specific model ID to benchmark. If omitted, benchmarks all registered models."
    ),
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

    from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn

    from velune.models.probes import ModelProber
    from velune.models.profile_cache import ModelProfileCache

    profile_cache = ModelProfileCache(Path(".velune") / "model_profiles.json")

    # Store benchmark results for auto-assignment
    benchmark_results = []

    if not cli_context.json_mode:
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        ) as progress:
            task_id = progress.add_task("[cyan]Benchmarking models...", total=len(models_to_probe))

            for model in models_to_probe:
                provider = provider_registry.get(model.provider_id)
                if not provider:
                    console.print(
                        f"[yellow]⊘[/yellow] {model.model_id}: Provider '{model.provider_id}' unavailable"
                    )
                    progress.advance(task_id)
                    continue

                progress.update(task_id, description=f"[cyan]Testing {model.model_id}...")

                prober = ModelProber(provider, model.model_id)
                results = await prober.run_all_probes()

                # Save to cache and registry
                profile_cache.set(model.model_id, model.provider_id, results)
                registry._apply_probe_results(model, results)

                coding = results["coding"]
                reasoning = results["reasoning"]
                instruction = results["instruction"]

                # Calculate speed score as average of latencies (lower is better)
                latencies = [
                    lat
                    for lat in [coding.latency_ms, reasoning.latency_ms, instruction.latency_ms]
                    if lat > 0
                ]
                avg_latency = sum(latencies) / len(latencies) if latencies else 0
                speed_score = max(0.0, 1.0 - (avg_latency / 3000.0))  # 3000ms = ~0 score

                benchmark_results.append(
                    {
                        "model": model,
                        "coding": coding,
                        "reasoning": reasoning,
                        "instruction": instruction,
                        "speed_score": speed_score,
                        "avg_latency_ms": avg_latency,
                    }
                )

                progress.advance(task_id)

        # Display results table
        _display_benchmark_results(cli_context, benchmark_results)

        # Auto-assign models based on scores
        _auto_assign_models(cli_context, registry, benchmark_results)

    else:
        # JSON mode: just collect and output raw results
        json_results = []

        for model in models_to_probe:
            provider = provider_registry.get(model.provider_id)
            if not provider:
                continue

            prober = ModelProber(provider, model.model_id)
            results = await prober.run_all_probes()

            profile_cache.set(model.model_id, model.provider_id, results)
            registry._apply_probe_results(model, results)

            coding = results["coding"]
            reasoning = results["reasoning"]
            instruction = results["instruction"]

            json_results.append(
                {
                    "model_id": model.model_id,
                    "provider_id": model.provider_id,
                    "results": {
                        "coding": {
                            "score": coding.score,
                            "latency_ms": coding.latency_ms,
                            "passed": coding.passed,
                        },
                        "reasoning": {
                            "score": reasoning.score,
                            "latency_ms": reasoning.latency_ms,
                            "passed": reasoning.passed,
                        },
                        "instruction": {
                            "score": instruction.score,
                            "latency_ms": instruction.latency_ms,
                            "passed": instruction.passed,
                        },
                    },
                }
            )

        print(json.dumps(json_results))


def _display_benchmark_results(cli_context: Any, benchmark_results: list[dict]) -> None:
    """Display benchmark results in a Rich table."""

    table = Table(title="Benchmark Results")
    table.add_column("Model", style="cyan", width=30)
    table.add_column("Provider", style="magenta", width=15)
    table.add_column("Coding", style="green", width=14)
    table.add_column("Reasoning", style="blue", width=14)
    table.add_column("Instruction", style="yellow", width=14)
    table.add_column("Speed", style="white", width=14)

    for result in benchmark_results:
        model = result["model"]
        coding = result["coding"]
        reasoning = result["reasoning"]
        instruction = result["instruction"]
        speed_score = result["speed_score"]
        result["avg_latency_ms"]

        def format_score(probe_result) -> str:
            if probe_result.latency_ms < 0:
                return "[red]Failed[/red]"
            color = "green" if probe_result.passed else "yellow"
            level_name = _score_to_level_name(probe_result.score)
            return f"[{color}]{probe_result.score:.2f}[/{color}]\n{level_name}"

        def format_speed(score_val: float) -> str:
            color = "green" if score_val > 0.7 else "yellow" if score_val > 0.4 else "red"
            level_name = _score_to_level_name(score_val)
            return f"[{color}]{score_val:.2f}[/{color}]\n{level_name}"

        table.add_row(
            model.model_id,
            model.provider_id,
            format_score(coding),
            format_score(reasoning),
            format_score(instruction),
            format_speed(speed_score),
        )

    console.print(table)
    console.print()


def _score_to_level_name(score: float) -> str:
    """Convert numerical score to capability level name."""
    if score >= 0.85:
        return "EXPERT"
    elif score >= 0.70:
        return "ADVANCED"
    elif score >= 0.50:
        return "INTERMEDIATE"
    elif score >= 0.25:
        return "BASIC"
    else:
        return "NONE"


def _auto_assign_models(cli_context: Any, registry: Any, benchmark_results: list[dict]) -> None:
    """Suggest model assignments based on benchmark results."""
    if not benchmark_results:
        return

    # Find best models for each capability
    best_coding = max(benchmark_results, key=lambda r: r["coding"].score)
    best_reasoning = max(benchmark_results, key=lambda r: r["reasoning"].score)
    best_speed = max(benchmark_results, key=lambda r: r["speed_score"])

    console.print("[bold]Suggested Model Assignments:[/bold]\n")
    console.print(
        f"  [cyan]Coding:[/cyan] {best_coding['model'].model_id} "
        f"({best_coding['coding'].score:.2f} - {_score_to_level_name(best_coding['coding'].score)})"
    )
    console.print(
        f"  [blue]Reasoning:[/blue] {best_reasoning['model'].model_id} "
        f"({best_reasoning['reasoning'].score:.2f} - {_score_to_level_name(best_reasoning['reasoning'].score)})"
    )
    console.print(
        f"  [green]Fast Model:[/green] {best_speed['model'].model_id} "
        f"({best_speed['speed_score']:.2f} - {_score_to_level_name(best_speed['speed_score'])})\n"
    )

    # Prompt for confirmation
    try:
        response = input("[bold]Apply these assignments?[/bold] [y/N] ").strip().lower()
        if response == "y":
            # Save assignments to config
            _save_model_assignments(
                best_coding["model"].model_id,
                best_reasoning["model"].model_id,
                best_speed["model"].model_id,
            )
            console.print("[green]Model assignments saved.[/green]")
        else:
            console.print("[dim]Assignments not applied.[/dim]")
    except (EOFError, KeyboardInterrupt):
        console.print("[dim]Assignments not applied.[/dim]")


def _save_model_assignments(coding_model: str, reasoning_model: str, fast_model: str) -> None:
    """Save model assignments to the project configuration."""
    import json
    from pathlib import Path

    config_file = Path(".velune") / "config.json"
    config_file.parent.mkdir(parents=True, exist_ok=True)

    config = {}
    if config_file.exists():
        try:
            config = json.loads(config_file.read_text())
        except Exception:
            pass

    config["model_assignments"] = {
        "coding_model": coding_model,
        "reasoning_model": reasoning_model,
        "fast_model": fast_model,
    }

    config_file.write_text(json.dumps(config, indent=2))
    console.print(f"[dim]Saved to {config_file}[/dim]")


@models_cmd.command("health")
def models_health(ctx: typer.Context) -> None:
    """Ping all registered models and report health status."""
    cli_context = ctx.obj if isinstance(ctx.obj, CLIContext) else None
    registry = cli_context.container.get("runtime.model_registry") if cli_context else None

    if registry is None:
        console.print("[red]Model registry is unavailable.[/red]")
        raise typer.Exit(code=1)

    all_models = registry.list_all()
    if not all_models:
        console.print("[yellow]No models registered. Run 'velune models scan' first.[/yellow]")
        return

    from velune.core.event_loop import submit

    submit(_models_health_async(cli_context, registry, all_models))


async def _models_health_async(
    cli_context: CLIContext,
    registry: Any,
    models: list[Any],
) -> None:
    import asyncio

    from velune.models.probes import FastProbe

    provider_registry = (
        cli_context.container.get("runtime.provider_registry") if cli_context else None
    )
    fast_probe = FastProbe()

    # Identify models that have a live provider to ping
    pingable: list[Any] = []
    ping_tasks = []
    for model in models:
        provider = provider_registry.get(model.provider_id) if provider_registry else None
        if provider:
            pingable.append(model)
            ping_tasks.append(fast_probe.ping(provider, model.model_id))

    responses = await asyncio.gather(*ping_tasks, return_exceptions=True) if ping_tasks else []

    pinged_keys = set()
    for model, result in zip(pingable, responses, strict=False):
        key = (model.provider_id, model.model_id)
        pinged_keys.add(key)
        model.health = "healthy" if result is True else "offline"
        registry.register(model)

    table = Table(title="Model Health Check")
    table.add_column("Provider", style="cyan")
    table.add_column("Model", style="green")
    table.add_column("Health", justify="center")
    table.add_column("Latency", style="yellow", justify="right")
    table.add_column("Location", style="dim", max_width=28)

    for model in models:
        key = (model.provider_id, model.model_id)
        health_str = getattr(model, "health", "unknown")
        if key not in pinged_keys:
            health_str = health_str or "unknown"

        lat = getattr(model, "last_latency_ms", None)
        lat_str = f"{lat:.0f}ms" if lat is not None else "[dim]—[/dim]"

        table.add_row(
            model.provider_id,
            model.model_id,
            _health_badge(health_str),
            lat_str,
            _short_location(getattr(model, "location", None)),
        )

    console.print(table)

    healthy = sum(1 for m in models if getattr(m, "health", "") == "healthy")
    total = len(models)
    not_pinged = total - len(pingable)
    summary = f"[dim]{healthy}/{total} models healthy"
    if not_pinged:
        summary += f" ({not_pinged} not pingable — no live provider)"
    summary += ".[/dim]"
    console.print(summary)


@models_cmd.command("show")
def models_show(
    ctx: typer.Context,
    model_id: str = typer.Argument(..., help="Model ID to inspect"),
) -> None:
    """Show detailed information for a single model."""
    cli_context = ctx.obj if isinstance(ctx.obj, CLIContext) else None
    registry = cli_context.container.get("runtime.model_registry") if cli_context else None

    if registry is None:
        console.print("[red]Model registry is unavailable.[/red]")
        raise typer.Exit(code=1)

    descriptor = registry.get(model_id)
    if descriptor is None:
        console.print(f"[red]Model '{model_id}' not found. Run 'velune models scan' first.[/red]")
        raise typer.Exit(code=1)

    from rich.panel import Panel

    from velune.core.types.model import CapabilityLevel

    lines = [
        f"[bold]Model ID:[/bold]      {descriptor.model_id}",
        f"[bold]Display Name:[/bold]  {descriptor.display_name}",
        f"[bold]Provider:[/bold]      {descriptor.provider_id}",
        f"[bold]Health:[/bold]        {_health_badge(getattr(descriptor, 'health', 'unknown'))}",
        f"[bold]Location:[/bold]      {getattr(descriptor, 'location', None) or '—'}",
        f"[bold]Context:[/bold]       {descriptor.context_length:,} tokens",
        f"[bold]Speed Tier:[/bold]    {descriptor.speed_tier}",
        f"[bold]Local:[/bold]         {'yes' if descriptor.is_local else 'no'}",
    ]
    lat = getattr(descriptor, "last_latency_ms", None)
    if lat is not None:
        lines.append(f"[bold]Last Latency:[/bold]  {lat:.0f}ms")
    if getattr(descriptor, "vram_required_gb", None):
        lines.append(f"[bold]VRAM Required:[/bold] {descriptor.vram_required_gb:.1f} GB")
    if descriptor.tags:
        lines.append(f"[bold]Tags:[/bold]          {', '.join(descriptor.tags)}")

    console.print(
        Panel(
            "\n".join(lines),
            title=f"[bold cyan]{descriptor.model_id}[/bold cyan]",
            expand=False,
        )
    )

    # Capability breakdown panel
    cap = descriptor.capabilities
    cap_lines = []
    for field_name in cap.model_fields:
        level = getattr(cap, field_name, CapabilityLevel.NONE)
        if level and level > CapabilityLevel.NONE:
            cap_lines.append(
                f"  [cyan]{field_name.replace('_', ' ').title():<22}[/cyan] {level.name}"
            )

    if cap_lines:
        console.print(
            Panel(
                "\n".join(cap_lines),
                title="Capabilities",
                expand=False,
            )
        )
    else:
        console.print(
            "[dim]No capability data. Run 'velune models scan --probe' to run empirical probes.[/dim]"
        )
