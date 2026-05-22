"""Typer application factory for Velune."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from velune import __version__
from velune.cli.context import CLIContext
from velune.cli.registry import register_commands
from velune.core.runtime import build_runtime
from velune.core.registry.container import ServiceContainer


def create_app() -> typer.Typer:
    """Create the root Typer application."""

    app = typer.Typer(
        name="velune",
        help="Terminal-first cognitive AI orchestration system",
        no_args_is_help=True,
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