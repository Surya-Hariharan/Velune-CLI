"""Config command - velune config set/get/show."""

from __future__ import annotations

from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel

from velune.cli.context import CLIContext

console = Console()

config_cmd = typer.Typer(help="Configuration management commands")


@config_cmd.command("set")
def config_set(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Configuration key (e.g. providers.default_provider)"),
    value: str = typer.Argument(..., help="Configuration value"),
) -> None:
    """Set a configuration value in velune.toml."""
    cli_context = ctx.obj if isinstance(ctx.obj, CLIContext) else None
    if not cli_context:
        if ctx.obj and getattr(ctx.obj, "json_mode", False):
            import json
            print(json.dumps({"error": "CLI context is uninitialized"}))
        else:
            from velune.cli.rendering.error_panel import render_error
            from velune.core.errors.catalog import WorkspaceNotInitializedError
            console.print(render_error(WorkspaceNotInitializedError(
                cause_override="CLI context was not properly initialized before this command."
            )))
        raise typer.Exit(1)

    config_path = cli_context.config_path or (cli_context.workspace / "velune.toml")

    # Load raw TOML
    import toml
    try:
        if config_path.exists():
            data = toml.load(config_path)
        else:
            data = {}
    except Exception as e:
        if cli_context.json_mode:
            import json
            print(json.dumps({"error": f"Failed to load existing config: {e}"}))
        else:
            from velune.cli.rendering.error_panel import render_unexpected_error
            console.print(render_unexpected_error(e))
        data = {}

    # Set the nested key
    parts = key.split(".")
    curr = data
    for part in parts[:-1]:
        if part not in curr or not isinstance(curr[part], dict):
            curr[part] = {}
        curr = curr[part]

    # Convert value to correct type (bool, int, float, str)
    typed_val: Any = value
    if value.lower() == "true":
        typed_val = True
    elif value.lower() == "false":
        typed_val = False
    else:
        try:
            if "." in value:
                typed_val = float(value)
            else:
                typed_val = int(value)
        except ValueError:
            pass

    curr[parts[-1]] = typed_val

    # Save back
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as f:
            toml.dump(data, f)
        if cli_context.json_mode:
            import json
            print(json.dumps({"success": True, "key": key, "value": typed_val, "path": str(config_path)}))
        else:
            console.print(f"[green]✓ Successfully set [bold]{key}[/bold] to [bold]{typed_val}[/bold] in {config_path}[/green]")
    except Exception as e:
        if cli_context.json_mode:
            import json
            print(json.dumps({"error": f"Failed to save config: {e}"}))
        else:
            from velune.cli.rendering.error_panel import render_unexpected_error
            console.print(render_unexpected_error(e))
        raise typer.Exit(1)


@config_cmd.command("get")
def config_get(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Configuration key (e.g. providers.default_provider)"),
) -> None:
    """Get a configuration value."""
    cli_context = ctx.obj if isinstance(ctx.obj, CLIContext) else None
    if not cli_context:
        if ctx.obj and getattr(ctx.obj, "json_mode", False):
            import json
            print(json.dumps({"error": "CLI context is uninitialized"}))
        else:
            from velune.cli.rendering.error_panel import render_error
            from velune.core.errors.catalog import WorkspaceNotInitializedError
            console.print(render_error(WorkspaceNotInitializedError(
                cause_override="CLI context was not properly initialized before this command."
            )))
        raise typer.Exit(1)

    # Fetch from the active loaded config object which is resolved and typed
    config = cli_context.config
    parts = key.split(".")
    curr: Any = config

    for part in parts:
        if hasattr(curr, part):
            curr = getattr(curr, part)
        elif isinstance(curr, dict) and part in curr:
            curr = curr[part]
        else:
            if cli_context.json_mode:
                import json
                print(json.dumps({"error": f"Key '{key}' not found in active configuration"}))
            else:
                console.print(f"[red]Key '{key}' not found in active configuration.[/red]")
            raise typer.Exit(1)

    if cli_context.json_mode:
        import json
        print(json.dumps({"key": key, "value": curr}))
    else:
        console.print(f"[bold]{key}[/bold] = {curr}")


@config_cmd.command("show")
def config_show(ctx: typer.Context) -> None:
    """Show all configuration."""
    cli_context = ctx.obj if isinstance(ctx.obj, CLIContext) else None

    if cli_context is None:
        if ctx.obj and getattr(ctx.obj, "json_mode", False):
            import json
            print(json.dumps({"error": "Configuration not yet loaded"}))
        else:
            console.print(Panel.fit("Configuration not yet loaded.", title="Configuration"))
        return

    config = cli_context.config
    if cli_context.json_mode:
        import json
        print(json.dumps({
            "project": {
                "name": config.project.name,
                "version": config.project.version,
            },
            "providers": {
                "default": config.providers.default_provider,
            },
            "workspace": {
                "index_on_init": config.workspace.index_on_init,
                "watch_files": config.workspace.watch_files,
                "git_aware": config.workspace.git_aware,
            },
            "telemetry": {
                "enabled": config.telemetry.enabled,
                "log_level": config.telemetry.log_level,
            }
        }))
    else:
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
