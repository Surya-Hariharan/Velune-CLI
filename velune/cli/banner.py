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
) -> None:
    # Title header
    console.print(f"[bold white]Velune {version}[/bold white]")
    console.print("[dim]Keys: /help for commands, /exit to exit[/dim]")

    # Linked workspace path
    pt_suffix = f" ({project_type_name})" if project_type_name and project_type_name != "Unknown" else ""
    console.print(f"Linked to: [white]{workspace_path}[/white]{pt_suffix}")

    # Hardware/System info
    vram_gb = hardware_profile.vram_total_gb
    gpu_part = (
        f"{hardware_profile.gpu_name} ({vram_gb:.0f}GB)"
        if hardware_profile.gpu_name and vram_gb is not None
        else (hardware_profile.gpu_name or "CPU only")
    )
    ram_part = f"{hardware_profile.total_ram_gb:.0f}GB RAM"

    provider_list = []
    if ollama_live:
        provider_list.append("ollama")
    for pid in configured_providers:
        if pid != "ollama":
            provider_list.append(pid)

    providers_str = ", ".join(provider_list) if provider_list else "none"
    console.print(f"System: {ram_part} • {gpu_part} • providers: {providers_str}")

    # Model
    if active_model_id:
        console.print(f"Model: [cyan]{active_model_id}[/cyan]")
    else:
        console.print("Model: [yellow]none — type /model to select[/yellow]")
    console.print()

    # Warnings/suggestions
    if hardware_profile.warnings:
        for warning in hardware_profile.warnings:
            console.print(f"[yellow]⚠[/yellow]  [yellow]{warning}[/yellow]")

    if hardware_profile.suggestions:
        for suggestion in hardware_profile.suggestions:
            console.print(f"[cyan]→[/cyan]  {suggestion}")

    if hardware_profile.warnings or hardware_profile.suggestions:
        console.print()
