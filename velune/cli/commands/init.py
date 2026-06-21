"""velune init — workspace initialisation command."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

app_init = typer.Typer()
console = Console()


@app_init.command("init")
def init_command(
    path: Path = typer.Argument(
        default=None,
        help="Path to project root (defaults to current directory)",
    ),
    provider: str = typer.Option(
        None,
        "--provider",
        "-p",
        help="Default provider: ollama | groq | openai | anthropic",
    ),
    skip_hardware: bool = typer.Option(
        False,
        "--skip-hardware-check",
        help="Skip hardware detection",
    ),
) -> None:
    """Initialize Velune in a project.

    Creates .velune/ config, detects hardware, checks providers,
    and prepares the workspace for first use.
    """
    workspace = (path or Path.cwd()).resolve()

    console.print(
        Panel(
            f"[bold cyan]Velune Init[/bold cyan]\n[dim]Setting up workspace: {workspace}[/dim]",
            border_style="cyan",
            padding=(0, 1),
        )
    )

    if not skip_hardware:
        _run_hardware_check()

    # .velune/ directory tree
    velune_dir = workspace / ".velune"
    velune_dir.mkdir(exist_ok=True)
    (velune_dir / "sessions").mkdir(exist_ok=True)
    (velune_dir / "index").mkdir(exist_ok=True)
    console.print("[green]✓[/green] Created .velune/ directory")

    # Project type detection
    try:
        import json as _json

        from velune.repository.project_type import ProjectTypeDetector

        _profile = ProjectTypeDetector().detect(workspace)
        (velune_dir / "project_profile.json").write_text(
            _json.dumps(
                {
                    "project_type": _profile.project_type.value,
                    "display_name": _profile.display_name,
                    "primary_language": _profile.primary_language,
                    "detected_frameworks": _profile.detected_frameworks,
                    "entry_points": _profile.entry_points,
                    "test_directories": _profile.test_directories,
                    "config_files": _profile.config_files,
                },
                indent=2,
            )
        )
        console.print(
            f"[green]✓[/green] Detected project type: [cyan]{_profile.display_name}[/cyan]"
        )
        if _profile.detected_frameworks:
            console.print(f"  [dim]Frameworks: {', '.join(_profile.detected_frameworks)}[/dim]")
    except Exception:
        pass

    # .veluneignore
    ignore_file = workspace / ".veluneignore"
    if not ignore_file.exists():
        from velune.repository.scanner import DEFAULT_VELUNEIGNORE

        ignore_file.write_text(DEFAULT_VELUNEIGNORE)
        console.print("[green]✓[/green] Created .veluneignore")

    # config.toml
    config_path = velune_dir / "config.toml"
    if not config_path.exists():
        default_provider = provider or _suggest_provider()
        config_content = f"""\
[project]
name = "{workspace.name}"
workspace = "."

[providers]
default_provider = "{default_provider}"

[council]
default_tier = "auto"

[memory]
enabled = true
"""
        config_path.write_text(config_content)
        console.print(
            f"[green]✓[/green] Created .velune/config.toml (provider: {default_provider})"
        )

    # .gitignore
    gitignore = workspace / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text()
        if ".velune/" not in content:
            with open(gitignore, "a") as f:
                f.write("\n# Velune\n.velune/\n")
            console.print("[green]✓[/green] Added .velune/ to .gitignore")

    console.print()
    console.print("[bold green]✓ Velune initialized.[/bold green]")
    console.print("[dim]Next steps:[/dim]")
    console.print("  [cyan]velune[/cyan]          — start the REPL")
    console.print("  [cyan]velune doctor[/cyan]   — verify configuration")


def _run_hardware_check() -> None:
    from velune.hardware.detector import HardwareDetector

    profile = HardwareDetector().detect()

    table = Table(border_style="dim", padding=(0, 1), show_header=False)
    table.add_column("Key", style="dim", width=20)
    table.add_column("Value", style="white")

    table.add_row("RAM", f"{profile.total_ram_gb:.0f} GB")
    table.add_row("GPU", profile.gpu_name or "None (CPU only)")
    table.add_row(
        "VRAM",
        f"{profile.vram_total_gb:.0f} GB" if profile.vram_total_gb else "—",
    )
    table.add_row("Tier", f"[cyan]{profile.tier.value}[/cyan]")
    table.add_row("Best local model", profile.recommended_model_size)

    console.print(table)

    for w in profile.warnings:
        console.print(f"  [yellow]⚠[/yellow] {w}")
    for s in profile.suggestions:
        console.print(f"  [dim]→ {s}[/dim]")


def _suggest_provider() -> str:
    try:
        import httpx

        r = httpx.get("http://localhost:11434/api/tags", timeout=2)
        if r.status_code == 200:
            return "ollama"
    except Exception:
        pass
    return "groq"
