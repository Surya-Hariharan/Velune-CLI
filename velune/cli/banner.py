from rich.console import Console


def render_startup_banner(
    console: Console,
    hardware_profile,
    configured_providers: list[str],
    ollama_live: bool,
    workspace_path: str,
    active_model_id: str | None,
    version: str = "0.1.0",
    project_type_name: str | None = None,
    runtime_profile_label: str | None = None,
) -> None:
    # 1. Logo and metadata
    vram_gb = hardware_profile.vram_total_gb
    gpu_part = (
        f"{hardware_profile.gpu_name} ({vram_gb:.0f}GB)"
        if hardware_profile.gpu_name and vram_gb is not None
        else (hardware_profile.gpu_name or "CPU only")
    )
    ram_part = f"{hardware_profile.total_ram_gb:.0f} GB RAM"
    tier_part = f"tier: {hardware_profile.tier.value}"
    hardware_info = f"{ram_part}  ·  {gpu_part}  ·  {tier_part}"
    if runtime_profile_label:
        hardware_info += f"  ·  profile: {runtime_profile_label}"

    pt_suffix = (
        f" ({project_type_name})" if project_type_name and project_type_name != "Unknown" else ""
    )
    workspace_info = f"{workspace_path}{pt_suffix}"

    console.print(f" [#c084fc]▐▛███▜▌[/#c084fc]   Velune v{version}")
    console.print(f"[#c084fc]▝▜█████▛▘[/#c084fc]  [dim]{hardware_info}[/dim]")
    console.print(f"  [#c084fc]▘▘ ▝▝[/#c084fc]    [dim]{workspace_info}[/dim]")
    console.print()

    # 2. Active model and providers
    provider_list = []
    if ollama_live:
        provider_list.append("ollama")
    for pid in configured_providers:
        if pid != "ollama":
            provider_list.append(pid)

    providers_str = ", ".join(provider_list) if provider_list else "none"

    if active_model_id:
        console.print(
            f" [#a78bfa]▎[/#a78bfa] Using [cyan]{active_model_id}[/cyan] (from velune.toml) · [dim]/model[/dim]"
        )
    else:
        console.print(
            " [#a78bfa]▎[/#a78bfa] [yellow]none — type /model to select[/yellow] · [dim]/model[/dim]"
        )

    console.print(f"   providers: [magenta]{providers_str}[/magenta] · [dim]/status[/dim]")

    # 3. Warnings/Suggestions
    if hardware_profile.warnings or hardware_profile.suggestions:
        console.print()
        if hardware_profile.warnings:
            for warning in hardware_profile.warnings:
                console.print(f"  [yellow]⚠[/yellow]  [yellow]{warning}[/yellow]")
        if hardware_profile.suggestions:
            for suggestion in hardware_profile.suggestions:
                console.print(f"  [cyan]→[/cyan]  {suggestion}")

    # 4. Recommendation box
    console.print()
    console.print(
        "[dim]────────────────────────────────────────────────────────────────────────────────[/dim]"
    )
    console.print(
        '  [bold cyan]>[/bold cyan] [dim]Try "how does[/dim] [cyan]<filepath>[/cyan] [dim]work?"[/dim]'
    )
    console.print(
        "[dim]────────────────────────────────────────────────────────────────────────────────[/dim]"
    )
    console.print("  [dim]? for shortcuts · /help for commands[/dim]")
