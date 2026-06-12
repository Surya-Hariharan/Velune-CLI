from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text


def render_startup_banner(
    console: Console,
    hardware_profile,
    configured_providers: list[str],
    ollama_live: bool,
    workspace_name: str,
    active_model_id: str | None,
    version: str = "0.1.0",
    project_type_name: str | None = None,
) -> None:
    # Title
    title = Text()
    title.append("Velune", style="bold white")
    title.append(f" {version}", style="dim white")

    # Status section
    status_items = []

    # Model status (primary)
    if active_model_id:
        status_items.append((Text("model", style="dim"), Text(active_model_id, style="cyan")))
    else:
        status_items.append((Text("model", style="dim"), Text("not selected", style="yellow")))

    # Workspace
    status_items.append((Text("workspace", style="dim"), Text(workspace_name, style="white")))

    # Hardware (condensed)
    vram_gb = hardware_profile.vram_total_gb
    gpu_part = (
        f"{hardware_profile.gpu_name} ({vram_gb:.0f}GB)"
        if hardware_profile.gpu_name and vram_gb is not None
        else (hardware_profile.gpu_name or "CPU only")
    )
    hw_text = f"{hardware_profile.total_ram_gb:.0f}GB RAM • {gpu_part}"
    status_items.append((Text("hardware", style="dim"), Text(hw_text, style="white")))

    # Providers (condensed)
    provider_list = []
    if ollama_live:
        provider_list.append("ollama")
    for pid in configured_providers:
        if pid != "ollama":
            provider_list.append(pid)

    if provider_list:
        providers_display = ", ".join(provider_list)
        provider_color = "green"
    else:
        providers_display = "none configured"
        provider_color = "red"

    status_items.append(
        (Text("providers", style="dim"), Text(providers_display, style=provider_color))
    )

    # Project type (if relevant)
    if project_type_name and project_type_name != "Unknown":
        status_items.append((Text("project", style="dim"), Text(project_type_name, style="green")))

    # Build status table
    body_lines = [title, Text()]

    for label, value in status_items:
        line = Text()
        line.append_text(label)
        line.append("  ")
        line.append_text(value)
        body_lines.append(line)

    body_lines.append(Text())
    hint = Text("Type a prompt or use ", style="dim")
    hint.append("/help", style="cyan")
    hint.append(" for commands", style="dim")
    body_lines.append(hint)

    console.print(
        Panel(
            Group(*body_lines),
            border_style="blue",
            padding=(0, 2),
        )
    )

    # Warnings/suggestions below panel (cleaner)
    if hardware_profile.warnings:
        console.print()
        for warning in hardware_profile.warnings:
            console.print(f"[yellow]⚠[/yellow]  [yellow]{warning}[/yellow]")

    if hardware_profile.suggestions:
        if not hardware_profile.warnings:
            console.print()
        for suggestion in hardware_profile.suggestions:
            console.print(f"[cyan]→[/cyan]  {suggestion}")

    if hardware_profile.warnings or hardware_profile.suggestions:
        console.print()
