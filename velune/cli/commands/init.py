"""velune init — the one-stop workspace initialisation command.

``velune init`` is the canonical way to prepare a project for Velune. In one
step it:

* detects your hardware (so model recommendations fit the machine),
* detects the project type and writes ``.velune/project_profile.json``,
* scaffolds the ``.velune/`` tree and ``.veluneignore``,
* writes ``.velune/config.toml`` (read by the runtime config loader),
* adds ``.velune/`` to ``.gitignore``, and
* builds the repository index so ``velune ask``/``run``/``chat`` work
  immediately.

The heavy indexing step is delegated to the *same* async routine that backs
``velune workspace init`` (:func:`velune.cli.commands.workspace._workspace_init_async`),
so the two commands can never drift: ``velune workspace init`` re-indexes an
existing workspace, and ``velune init`` layers the first-run scaffolding +
config on top of that shared core.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from velune.cli import design
from velune.cli.context import CLIContext

console = Console()


def init_command(
    ctx: typer.Context,
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
    no_index: bool = typer.Option(
        False,
        "--no-index",
        help="Scaffold config only; skip building the repository index",
    ),
) -> None:
    """Initialize Velune in a project — scaffold config, detect environment, build the index."""
    workspace = (path or Path.cwd()).resolve()

    console.print(
        Panel(
            f"[bold {design.ACCENT}]Velune Init[/bold {design.ACCENT}]\n"
            f"[{design.MUTED}]Setting up workspace: {workspace}[/{design.MUTED}]",
            border_style=design.ACCENT,
            padding=(0, 1),
        )
    )

    if not skip_hardware:
        _run_hardware_check()

    # .velune/ directory tree — kept in sync with `velune workspace init`.
    velune_dir = workspace / ".velune"
    for sub in ("", "sessions", "index", "memory", "retrieval", "snapshots"):
        (velune_dir / sub).mkdir(parents=True, exist_ok=True)
    console.print(f"[{design.OK}]Created .velune/ directory[/{design.OK}]")

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
            f"[{design.OK}]Detected project type:[/{design.OK}]"
            f" [{design.ACCENT}]{_profile.display_name}[/{design.ACCENT}]"
        )
        if _profile.detected_frameworks:
            console.print(
                f"  [{design.MUTED}]Frameworks: {', '.join(_profile.detected_frameworks)}[/{design.MUTED}]"
            )
    except Exception:
        pass

    # .veluneignore
    ignore_file = workspace / ".veluneignore"
    if not ignore_file.exists():
        from velune.repository.scanner import DEFAULT_VELUNEIGNORE

        ignore_file.write_text(DEFAULT_VELUNEIGNORE, encoding="utf-8")
        console.print(f"[{design.OK}]Created .veluneignore[/{design.OK}]")

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
        config_path.write_text(config_content, encoding="utf-8")
        console.print(
            f"[{design.OK}]Created .velune/config.toml[/{design.OK}]"
            f" [{design.MUTED}](provider: {default_provider})[/{design.MUTED}]"
        )

    # .gitignore
    gitignore = workspace / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8", errors="ignore")
        if ".velune/" not in content:
            with open(gitignore, "a", encoding="utf-8") as f:
                f.write("\n# Velune\n.velune/\n")
            console.print(f"[{design.OK}]Added .velune/ to .gitignore[/{design.OK}]")

    # Register in the global workspace registry so `velune project list` finds it.
    try:
        from velune.cli.workspaces import WorkspaceRegistry

        WorkspaceRegistry().register(workspace)
    except Exception:
        pass

    # Build the repository index by delegating to the shared workspace-init core.
    # Skipped when --no-index is passed, or when no runtime container is available
    # (defensive — e.g. a degraded bootstrap) in which case we finish scaffold-only.
    cli_context = ctx.obj if isinstance(ctx.obj, CLIContext) else None
    if no_index or cli_context is None:
        console.print()
        console.print(f"[bold {design.OK}]Velune initialized.[/bold {design.OK}]")
        if cli_context is None and not no_index:
            console.print(
                f"[{design.MUTED}]Index not built (runtime unavailable) —"
                f" run [bold]velune workspace init[/bold] once a provider is configured.[/{design.MUTED}]"
            )
        _print_next_steps(indexed=False)
        return

    from velune.cli.commands.workspace import _workspace_init_async
    from velune.core.event_loop import submit

    # _workspace_init_async builds the AST index and prints its own success panel.
    submit(_workspace_init_async(cli_context, workspace, velune_dir, force=False))
    _print_next_steps(indexed=True)


def _print_next_steps(*, indexed: bool) -> None:
    console.print()
    console.print(f"[{design.MUTED}]Next steps:[/{design.MUTED}]")
    console.print(
        f"  [{design.ACCENT}]velune[/{design.ACCENT}]"
        f"          [{design.MUTED}]— start the interactive session[/{design.MUTED}]"
    )
    console.print(
        f'  [{design.ACCENT}]velune ask "..."[/{design.ACCENT}]'
        f" [{design.MUTED}]— ask a one-off question[/{design.MUTED}]"
    )
    if not indexed:
        console.print(
            f"  [{design.ACCENT}]velune workspace init[/{design.ACCENT}]"
            f" [{design.MUTED}]— build the repository index for codebase-aware answers[/{design.MUTED}]"
        )
    console.print(
        f"  [{design.ACCENT}]velune doctor[/{design.ACCENT}]"
        f"   [{design.MUTED}]— verify configuration[/{design.MUTED}]"
    )


def _run_hardware_check() -> None:
    from velune.hardware.detector import HardwareDetector

    profile = HardwareDetector().detect()

    table = Table(border_style=design.FAINT, padding=(0, 1), show_header=False)
    table.add_column("Key", style=design.MUTED, width=20)
    table.add_column("Value", style=design.WHITE)

    table.add_row("RAM", f"{profile.total_ram_gb:.0f} GB")
    table.add_row("GPU", profile.gpu_name or "None (CPU only)")
    table.add_row(
        "VRAM",
        f"{profile.vram_total_gb:.0f} GB" if profile.vram_total_gb else "—",
    )
    table.add_row("Tier", f"[{design.ACCENT}]{profile.tier.value}[/{design.ACCENT}]")
    table.add_row("Best local model", profile.recommended_model_size)

    console.print(table)

    for w in profile.warnings:
        console.print(f"  [{design.WARN}]{w}[/{design.WARN}]")
    for s in profile.suggestions:
        console.print(f"  [{design.MUTED}]→ {s}[/{design.MUTED}]")


def _suggest_provider() -> str:
    try:
        import httpx

        r = httpx.get("http://localhost:11434/api/tags", timeout=2)
        if r.status_code == 200:
            return "ollama"
    except Exception:
        pass
    return "groq"
