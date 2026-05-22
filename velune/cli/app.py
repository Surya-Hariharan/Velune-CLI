"""Typer application factory for Velune."""

from __future__ import annotations

import sys
import time

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from pathlib import Path

import typer
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from velune import __version__
from velune.cli.context import CLIContext
from velune.cli.registry import register_commands
from velune.core.runtime import build_runtime
from velune.core.registry.container import ServiceContainer


def _startup_frames(workspace: Path, config_path: Path | None) -> list[Panel]:
    banner = """
██╗   ██╗███████╗██╗     ██╗   ██╗███╗   ██╗███████╗
██║   ██║██╔════╝██║     ██║   ██║████╗  ██║██╔════╝
██║   ██║█████╗  ██║     ██║   ██║██╔██╗ ██║█████╗  
╚██╗ ██╔╝██╔══╝  ██║     ██║   ██║██║╚██╗██║██╔══╝  
 ╚████╔╝ ███████╗███████╗╚██████╔╝██║ ╚████║███████╗
  ╚═══╝  ╚══════╝╚══════╝ ╚═════╝ ╚═╝  ╚═══╝╚══════╝
""".strip("\n")

    frames: list[Panel] = []
    lines = banner.splitlines()
    for index in range(1, len(lines) + 1):
        body = "\n".join(lines[:index])
        if index == len(lines):
            body += "\n\n[bold cyan]Welcome to Velune CLI![/bold cyan]\n[dim]v" + __version__ + "[/dim]\n\n[bold]What would you like to build today?[/bold]"
        frames.append(
            Panel(
                Text.from_markup(body),
                title="Velune",
                border_style="cyan",
                padding=(1, 2),
            )
        )

    frames.append(
        Panel(
            Text.from_markup(
                "[bold cyan]Welcome to Velune CLI![/bold cyan]\n"
                f"[dim]v{__version__}[/dim]\n\n"
                "[bold]What would you like to build today?[/bold]"
            ),
            title="Velune",
            border_style="cyan",
            padding=(1, 2),
        )
    )
    return frames


def _show_startup_animation(console: Console, workspace: Path, config_path: Path | None) -> None:
    frames = _startup_frames(workspace, config_path)
    with Live(frames[0], console=console, refresh_per_second=12, transient=True) as live:
        for frame in frames[1:]:
            live.update(frame)
            time.sleep(0.08)


def create_app() -> typer.Typer:
    """Create the root Typer application."""

    app = typer.Typer(
        name="velune",
        help="Terminal-first cognitive AI orchestration system",
        no_args_is_help=False,
        add_completion=True,
        rich_markup_mode="rich",
    )

    @app.callback(invoke_without_command=True)
    def main(
        ctx: typer.Context,
        workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w", help="Workspace root"),
        config_path: Path | None = typer.Option(None, "--config", "-c", help="Explicit velune.toml path"),
        verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
        version: bool = typer.Option(False, "--version", help="Show version and exit"),
    ) -> None:
        """Initialize process-wide runtime state for every CLI invocation."""

        if version:
            Console().print(f"Velune v{__version__}")
            raise typer.Exit()

        runtime = build_runtime(workspace=workspace, config_path=config_path, verbose=verbose)
        ctx.obj = CLIContext(
            workspace=workspace,
            config_path=config_path,
            verbose=verbose,
            runtime=runtime,
        )

        if ctx.invoked_subcommand is None:
            _show_startup_animation(runtime.console, workspace, config_path)
            runtime.console.print(
                Panel(
                    "\n".join(
                        [
                            "[bold]Velune is ready.[/bold]",
                            f"Workspace: {workspace}",
                            f"Config: {config_path or 'auto-discovered'}",
                            "Use [bold]velune ask[/bold], [bold]velune models scan[/bold], or [bold]velune memory stats[/bold] to start.",
                        ]
                    ),
                    title="Startup",
                )
            )

    register_commands(app, ServiceContainer())
    return app


app = create_app()