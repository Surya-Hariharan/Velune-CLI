"""Config command - velune config set/get."""

import typer
from rich.console import Console
from rich.panel import Panel

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
def config_show() -> None:
    """Show all configuration."""
    console.print("[yellow]Current configuration:[/yellow]")
    console.print(Panel.fit(
        "Configuration not yet loaded.\n"
        "Edit velune.toml in your workspace root.",
        title="Configuration",
    ))
