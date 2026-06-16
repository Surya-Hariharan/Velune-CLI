"""MCP command - velune mcp-serve and velune mcp connect."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from velune.cli.context import CLIContext
from velune.core.event_loop import submit
from velune.core.runtime import build_runtime
from velune.kernel.config import ConfigLoader
from velune.mcp.client import VeluneMCPClient
from velune.mcp.server import VeluneMCPServer

console = Console()
mcp_cmd = typer.Typer(help="Connect to or expose an MCP server.")


@mcp_cmd.command("connect")
def mcp_connect(
    ctx: typer.Context,
    server_url: str = typer.Argument(..., help="SSE URL of the external MCP server"),
    name: str = typer.Argument(..., help="Name of the MCP server"),
) -> None:
    """Connect to an external MCP server and list its tools."""
    console.print(f"[cyan]Connecting to external MCP server '{name}' at {server_url}...[/cyan]")

    cli_context = ctx.obj if isinstance(ctx.obj, CLIContext) else None
    allowed_hosts: list[str] = []
    try:
        loader = ConfigLoader(cli_context.config_path if cli_context else None)
        allowed_hosts = list(loader.load().mcp.allowed_hosts)
    except Exception:
        allowed_hosts = []

    client = VeluneMCPClient(server_url, name, allowed_hosts=allowed_hosts or None)

    async def _connect_and_list():
        try:
            tools = await client.connect()
            console.print(f"[green]✓ Connected to {name} successfully![/green]")
            console.print(f"[bold]Exposed Tools ({len(tools)}):[/bold]")
            for tool in tools:
                desc = tool.get("description", "No description")
                if len(desc) > 80:
                    desc = desc[:77] + "..."
                console.print(f"  - [cyan]{name}_{tool['name']}[/cyan]: {desc}")
        except Exception as e:
            console.print(f"[red]Error connecting to MCP server: {e}[/red]")
            raise typer.Exit(1)
        finally:
            await client.disconnect()

    submit(_connect_and_list())


@mcp_cmd.command("serve")
def mcp_serve_subcmd(ctx: typer.Context) -> None:
    """Start Velune as an MCP server for Claude Desktop / VS Code."""
    cli_context = ctx.obj if isinstance(ctx.obj, CLIContext) else None
    workspace = cli_context.workspace if cli_context else Path.cwd()
    config_path = cli_context.config_path if cli_context else None

    container = build_runtime(workspace, config_path=config_path).container
    tool_registry = container.get("runtime.tool_registry")
    server = VeluneMCPServer(tool_registry)

    import logging

    logging.getLogger("velune").setLevel(logging.WARNING)

    submit(server.run_stdio())


def mcp_serve(ctx: typer.Context) -> None:
    """Start Velune as an MCP server for Claude Desktop / VS Code."""
    cli_context = ctx.obj if isinstance(ctx.obj, CLIContext) else None
    workspace = cli_context.workspace if cli_context else Path.cwd()
    config_path = cli_context.config_path if cli_context else None

    container = build_runtime(workspace, config_path=config_path).container
    tool_registry = container.get("runtime.tool_registry")
    server = VeluneMCPServer(tool_registry)

    import logging

    logging.getLogger("velune").setLevel(logging.WARNING)

    submit(server.run_stdio())
