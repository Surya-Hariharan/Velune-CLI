"""Config command - velune config set/get."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel

from velune.cli.context import CLIContext

console = Console()

config_cmd = typer.Typer(help="Configuration management commands")


@config_cmd.command("set")
def config_set(
    key: str = typer.Argument(..., help="Configuration key"),
    value: str = typer.Argument(..., help="Configuration value"),
) -> None:
    """Set a configuration value."""
    console.print(f"[yellow]Setting {key} = {value}[/yellow]")
    console.print("[yellow]Configuration management not yet implemented.[/yellow]")


@config_cmd.command("get")
def config_get(
    key: str = typer.Argument(..., help="Configuration key"),
) -> None:
    """Get a configuration value."""
    console.print(f"[yellow]Getting {key}[/yellow]")
    console.print("[yellow]Configuration management not yet implemented.[/yellow]")


@config_cmd.command("show")
def config_show(ctx: typer.Context) -> None:
    """Show all configuration."""
    cli_context = ctx.obj if isinstance(ctx.obj, CLIContext) else None

    if cli_context is None:
        console.print(Panel.fit("Configuration not yet loaded.", title="Configuration"))
        return

    config = cli_context.config
    console.print(
        Panel.fit(
            f"project.name = {config.project.name}\n"
            f"project.version = {config.project.version}\n"
            f"providers.default = {config.providers.default_provider}\n"
            f"workspace.index_on_init = {config.workspace.index_on_init}\n"
            f"workspace.watch_files = {config.workspace.watch_files}\n"
            f"workspace.git_aware = {config.workspace.git_aware}\n"
            f"telemetry.enabled = {config.telemetry.enabled}\n"
            f"telemetry.log_level = {config.telemetry.log_level}",
            title="Configuration",
        )
    )
