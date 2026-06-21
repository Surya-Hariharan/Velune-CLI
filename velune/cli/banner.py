from rich.console import Console
from rich.text import Text

from velune import __version__


def render_startup_banner(
    console: Console,
    hardware_profile,
    configured_providers: list[str],
    ollama_live: bool,
    workspace_path: str,
    active_model_id: str | None,
    version: str = __version__,
    project_type_name: str | None = None,
    runtime_profile_label: str | None = None,
) -> None:
    from velune.cli import design

    console.print()

    # 1. Elegant logo with organic brand identity
    logo_line1 = Text("  ◆  ", style=f"bold {design.ACCENT}") + Text(
        "V e l u n e", style=f"bold {design.ACCENT}"
    )
    logo_line2 = Text("      ", style=f"{design.ACCENT}") + Text(
        "Local-first AI Orchestrator", style=f"{design.INFO}"
    )
    console.print(logo_line1)
    console.print(logo_line2)
    console.print()

    # 2. Hardware and environment info with better visual hierarchy
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
        hardware_info += f"  ·  {design.SPEED}profile{design.SPEED}: {runtime_profile_label}"

    pt_suffix = (
        f" ({project_type_name})" if project_type_name and project_type_name != "Unknown" else ""
    )
    workspace_info = f"{workspace_path}{pt_suffix}"

    console.print(f"  [dim]{hardware_info}[/dim]")
    console.print(f"  [{design.MUTED}]{workspace_info}[/{design.MUTED}]")
    console.print()

    # 3. Model configuration with control indicators
    provider_list = []
    if ollama_live:
        provider_list.append(f"[{design.GREEN}]◆ ollama[/{design.GREEN}]")
    for pid in configured_providers:
        if pid != "ollama":
            provider_list.append(f"[{design.ACCENT}]◆ {pid}[/{design.ACCENT}]")

    providers_str = (
        "  ".join(provider_list)
        if provider_list
        else f"[{design.MUTED}]none configured[/{design.MUTED}]"
    )

    if active_model_id:
        model_indicator = Text("⚡ ", style=design.ENERGY) + Text(
            f"{active_model_id}", style=f"bold {design.ACCENT}"
        )
        console.print(f"  Active Model:  {model_indicator}  [dim](/model to change)[/dim]")
    else:
        console.print(
            f"  Active Model:  [{design.WARN}]none selected[/{design.WARN}]"
            f"  [dim](/model to select)[/dim]"
        )

    console.print(f"  Providers:  {providers_str}  [dim](/status to check)[/dim]")
    console.print()

    # 4. Warnings/Suggestions with organic styling
    if hardware_profile.warnings or hardware_profile.suggestions:
        if hardware_profile.warnings:
            for warning in hardware_profile.warnings:
                console.print(f"  [{design.WARN}]⚠ {warning}[/{design.WARN}]")
        if hardware_profile.suggestions:
            for suggestion in hardware_profile.suggestions:
                console.print(f"  [{design.INFO}]→ {suggestion}[/{design.INFO}]")
        console.print()

    # 5. Gentle separator with call-to-action
    rule = "─" * max(24, min(console.width - 2, 76))
    console.print(f"  [{design.FAINT}]{rule}[/{design.FAINT}]")
    console.print(
        f"  [{design.ACCENT}]?[/{design.ACCENT}] Try orchestration: [dim]how does[/dim] [{design.INFO}]<filepath>[/{design.INFO}] [dim]work?[/dim]"
    )
    console.print(f"  [{design.FAINT}]{rule}[/{design.FAINT}]")
    console.print()
    console.print("  [dim]Type /help for available commands · /setup to configure providers[/dim]")
    console.print()
