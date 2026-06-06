from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

_TIER_COLORS: dict[str, str] = {
    "critical": "red",
    "low":      "red",
    "marginal": "yellow",
    "capable":  "green",
    "powerful": "green",
    "elite":    "cyan",
}


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
    # Header
    header = Text()
    header.append("velune ", style="bold cyan")
    header.append(f"v{version}", style="dim")
    header.append("  ·  ", style="dim")
    header.append(workspace_name, style="bold white")

    # Hardware
    tier = hardware_profile.tier.value
    color = _TIER_COLORS.get(tier, "white")
    vram_gb = hardware_profile.vram_total_gb
    gpu_part = (
        f"{hardware_profile.gpu_name} ({vram_gb:.0f} GB)"
        if hardware_profile.gpu_name and vram_gb is not None
        else (hardware_profile.gpu_name if hardware_profile.gpu_name else "CPU only")
    )
    hw_line = Text()
    hw_line.append("hardware  ", style="dim")
    hw_line.append(f"{hardware_profile.total_ram_gb:.0f} GB RAM", style="white")
    hw_line.append("  ·  ", style="dim")
    hw_line.append(gpu_part, style="white")
    hw_line.append("  ·  ", style="dim")
    hw_line.append(f"tier: {tier}", style=color)

    # Providers
    parts: list[str] = []
    if ollama_live:
        parts.append("[green]● ollama[/green]")
    for pid in configured_providers:
        if pid != "ollama":
            parts.append(f"[green]● {pid}[/green]")
    if not parts:
        parts.append("[red]✗ no providers[/red]")
    providers_text = Text.from_markup("providers  " + "  ".join(parts))

    # Model
    if active_model_id:
        model_text = Text.from_markup(
            f"[dim]model[/dim]     [cyan]{active_model_id}[/cyan]"
        )
    else:
        model_text = Text.from_markup(
            "[dim]model[/dim]     [yellow]none — type /model to select[/yellow]"
        )

    # Project type (optional)
    project_text = None
    if project_type_name and project_type_name != "Unknown":
        project_text = Text.from_markup(
            f"[dim]project[/dim]   [green]{project_type_name}[/green]"
        )

    # Hint
    hint = Text("type a prompt or ", style="dim")
    hint.append("/help", style="cyan")
    hint.append(" for commands", style="dim")

    body_items = [header, hw_line, providers_text, model_text]
    if project_text:
        body_items.append(project_text)
    body_items.append(hint)

    console.print(Panel(
        Group(*body_items),
        border_style="cyan",
        padding=(0, 1),
    ))

    for warning in hardware_profile.warnings:
        console.print(f"  [yellow]⚠[/yellow] [dim]{warning}[/dim]")
    for suggestion in hardware_profile.suggestions:
        console.print(f"  [dim]→ {suggestion}[/dim]")
    if hardware_profile.warnings or hardware_profile.suggestions:
        console.print()
