"""CLI commands for MCP server: start, status, and configuration."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from velune.cli.commands.base import BaseCommand
from velune.core.trace import TracedLogger

logger = TracedLogger("velune.cli.commands.mcp")
console = Console()

MCP_PID_FILE = Path.home() / ".velune" / "mcp.pid"
MCP_LOG_FILE = Path.home() / ".velune" / "mcp.log"


def get_mcp_group() -> click.Group:
    """Return Click group for MCP commands."""

    @click.group("mcp", help="Manage Velune MCP server for Claude Desktop")
    def mcp_group():
        pass

    @mcp_group.command("start")
    @click.option(
        "--workspace",
        "-w",
        type=click.Path(exists=True),
        default=None,
        help="Workspace path (defaults to current directory)",
    )
    @click.option(
        "--host",
        default="127.0.0.1",
        help="HTTP host for SSE transport",
    )
    @click.option(
        "--port",
        type=int,
        default=7777,
        help="HTTP port for SSE transport",
    )
    @click.option(
        "--transport",
        type=click.Choice(["stdio", "http", "both"]),
        default="stdio",
        help="Transport type",
    )
    @click.option(
        "--background",
        is_flag=True,
        help="Run in background",
    )
    def start_mcp(workspace: str | None, host: str, port: int, transport: str, background: bool):
        """Start the Velune MCP server."""
        workspace_path = Path(workspace or Path.cwd())

        console.print(
            Panel(
                f"[bold green]Starting Velune MCP Server[/bold green]\n"
                f"Workspace: {workspace_path}\n"
                f"Transport: {transport}",
                title="MCP Server",
            )
        )

        if background:
            _start_background(workspace_path, host, port, transport)
        else:
            _start_foreground(workspace_path, host, port, transport)

    @mcp_group.command("status")
    def status_mcp():
        """Show MCP server status."""
        if not MCP_PID_FILE.exists():
            console.print("[red]MCP server not running[/red]")
            return

        try:
            pid = int(MCP_PID_FILE.read_text().strip())
            os.kill(pid, 0)  # Check if process exists
            console.print(
                Panel(
                    f"[bold green]MCP Server Running[/bold green]\n"
                    f"PID: {pid}\n"
                    f"Log: {MCP_LOG_FILE}",
                    title="Status",
                )
            )
        except (ProcessLookupError, OSError, ValueError):
            console.print("[yellow]Process not found[/yellow]")
            MCP_PID_FILE.unlink(missing_ok=True)

    @mcp_group.command("stop")
    def stop_mcp():
        """Stop the MCP server."""
        if not MCP_PID_FILE.exists():
            console.print("[yellow]MCP server not running[/yellow]")
            return

        try:
            pid = int(MCP_PID_FILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            time.sleep(1)
            console.print(f"[green]Stopped MCP server (PID {pid})[/green]")
            MCP_PID_FILE.unlink()
        except Exception as e:
            console.print(f"[red]Failed to stop server: {e}[/red]")

    @mcp_group.command("config")
    @click.option(
        "--workspace",
        "-w",
        type=click.Path(exists=True),
        default=None,
        help="Workspace path",
    )
    def config_mcp(workspace: str | None):
        """Generate Claude Desktop config snippet."""
        workspace_path = Path(workspace or Path.cwd()).resolve()

        config = {
            "mcpServers": {
                "velune": {
                    "command": "velune",
                    "args": ["mcp", "start", "--workspace", str(workspace_path)],
                }
            }
        }

        console.print(
            Panel(
                Syntax(
                    json.dumps(config, indent=2),
                    "json",
                    theme="monokai",
                    line_numbers=True,
                ),
                title="Claude Desktop Config",
            )
        )

        console.print(
            Panel(
                "[bold]Setup:[/bold]\n"
                "1. Edit ~/.config/Claude/claude_desktop_config.json\n"
                "2. Add the config above\n"
                "3. Restart Claude Desktop\n"
                "4. Velune tools appear in MCP servers",
                title="Next Steps",
            )
        )

    return mcp_group


def _start_foreground(workspace_path: Path, host: str, port: int, transport: str) -> None:
    """Start MCP server in foreground."""
    try:
        from velune.mcp.server import VeluneMCPServer
        from velune.tools.base.registry import ToolRegistry

        tool_registry = ToolRegistry()
        server = VeluneMCPServer(
            tool_registry=tool_registry,
            workspace_path=workspace_path,
        )

        if transport == "stdio":
            asyncio.run(server.run_stdio())
        elif transport == "http":
            asyncio.run(server.run_http(host, port))
        elif transport == "both":
            asyncio.run(_run_both_transports(server, host, port))

    except Exception as e:
        logger.error(f"MCP server failed: {e}")
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def _start_background(workspace_path: Path, host: str, port: int, transport: str) -> None:
    """Start MCP server in background."""
    try:
        MCP_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        MCP_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            sys.executable,
            "-m",
            "velune",
            "mcp",
            "start",
            "--workspace",
            str(workspace_path),
        ]

        with open(MCP_LOG_FILE, "w") as log_f:
            proc = subprocess.Popen(
                cmd,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        MCP_PID_FILE.write_text(str(proc.pid))
        console.print(
            Panel(
                f"[bold green]MCP Server Started[/bold green]\n"
                f"PID: {proc.pid}\n"
                f"Log: {MCP_LOG_FILE}",
                title="Background Server",
            )
        )
    except Exception as e:
        logger.error(f"Failed to start background server: {e}")
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


async def _run_both_transports(server, host: str, port: int) -> None:
    """Run stdio and HTTP transports concurrently."""
    stdio_task = asyncio.create_task(server.run_stdio())
    http_task = asyncio.create_task(server.run_http(host, port))
    await asyncio.gather(stdio_task, http_task)
