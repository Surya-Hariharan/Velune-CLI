"""MCP (Model Context Protocol) slash command handlers: /mcp."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from rich.table import Table

from velune.cli import design

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL

_log = logging.getLogger("velune.cli.handlers.mcp")


async def cmd_mcp(repl: VeluneREPL, args: str) -> None:
    """Inspect and manage MCP server connections."""
    parts = args.strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else "servers"
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub in ("", "servers"):
        await _mcp_show_servers(repl)
    elif sub == "tools":
        _mcp_show_tools(repl, server_filter=rest or None)
    elif sub == "resources":
        _mcp_show_resources(repl, server_filter=rest or None)
    elif sub == "connect":
        if not rest:
            repl.console.print("[yellow]Usage: /mcp connect <server-name>[/yellow]")
            return
        repl.console.print(f"[dim]Connecting to MCP server '{rest}'...[/dim]")
        ok = await repl._mcp_registry.connect(rest)
        if ok:
            tools = repl._mcp_registry.tools_for_server(rest)
            repl.console.print(
                f"[green]Connected to [bold]{rest}[/bold] ({len(tools)} tool(s)).[/green]"
            )
        else:
            status = next((s for s in repl._mcp_registry.status() if s["name"] == rest), {})
            repl.console.print(
                f"[red]Failed to connect to [bold]{rest}[/bold]: "
                f"{status.get('error', 'unknown error')}[/red]"
            )
    elif sub == "disconnect":
        if not rest:
            repl.console.print("[yellow]Usage: /mcp disconnect <server-name>[/yellow]")
            return
        await repl._mcp_registry.disconnect(rest)
        repl.console.print(f"[dim]Disconnected from [bold]{rest}[/bold].[/dim]")
    elif sub == "refresh":
        if not rest:
            repl.console.print("[yellow]Usage: /mcp refresh <server-name>[/yellow]")
            return
        ok = await repl._mcp_registry.refresh_tools(rest)
        if ok:
            tools = repl._mcp_registry.tools_for_server(rest)
            repl.console.print(
                f"[green]Refreshed [bold]{rest}[/bold] ({len(tools)} tool(s)).[/green]"
            )
        else:
            repl.console.print(f"[yellow]Could not refresh '{rest}' — is it connected?[/yellow]")
    else:
        repl.console.print(
            "[yellow]Unknown sub-command. "
            "Try: /mcp servers | tools | resources | connect <name> | "
            "disconnect <name> | refresh <name>[/yellow]"
        )


async def _mcp_show_servers(repl: VeluneREPL) -> None:
    from velune.cli import ui

    rows = repl._mcp_registry.status()
    if not rows:
        repl.console.print(
            ui.notification(
                "No MCP servers configured. Create .mcp.json in the workspace to add servers.",
                kind="info",
            )
        )
        return

    table = Table(
        box=None,
        pad_edge=False,
        padding=design.PADDING_DEFAULT,
    )
    for col in ("Name", "State", "Transport", "Endpoint", "Tools", "Resources"):
        table.add_column(col, style=design.MUTED)

    _state_style = {
        "connected": "green",
        "connecting": "yellow",
        "disconnected": "dim",
        "error": "red",
    }

    for row in rows:
        state = row["state"]
        style = _state_style.get(state, "dim")
        error = f" ({row['error'][:40]})" if row.get("error") else ""
        table.add_row(
            row["name"],
            f"[{style}]{state}{error}[/{style}]",
            row["transport"],
            row["endpoint"][:38],
            str(row["tools"]),
            str(row["resources"]),
        )

    repl.console.print(ui.header("MCP Servers"))
    repl.console.print(ui.rule())
    repl.console.print(table)
    repl.console.print(
        "\n[dim]Sub-commands: /mcp tools | /mcp resources | /mcp connect <name> | "
        "/mcp disconnect <name> | /mcp refresh <name>[/dim]"
    )


def _mcp_show_tools(repl: VeluneREPL, server_filter: str | None = None) -> None:
    from velune.cli import ui

    all_tools = repl._mcp_registry.all_tools()
    if server_filter:
        all_tools = [t for t in all_tools if t.server_name == server_filter]

    if not all_tools:
        label = f" from '{server_filter}'" if server_filter else ""
        repl.console.print(ui.notification(f"No tools available{label}.", kind="info"))
        return

    table = Table(
        box=None,
        pad_edge=False,
        padding=design.PADDING_DEFAULT,
    )
    for col in ("Server", "Tool", "Description"):
        table.add_column(col, style=design.MUTED)

    for tool in all_tools:
        desc = tool.description
        if len(desc) > 80:
            desc = desc[:77] + "..."
        table.add_row(tool.server_name, tool.name, desc)

    repl.console.print(ui.header("MCP Tools"))
    repl.console.print(ui.rule())
    repl.console.print(table)
    repl.console.print(f"\n[dim]{len(all_tools)} tool(s) available.[/dim]")


def _mcp_show_resources(repl: VeluneREPL, server_filter: str | None = None) -> None:
    from velune.cli import ui

    all_resources = repl._mcp_registry.all_resources()
    if server_filter:
        all_resources = [r for r in all_resources if r.server_name == server_filter]

    if not all_resources:
        label = f" from '{server_filter}'" if server_filter else ""
        repl.console.print(ui.notification(f"No resources available{label}.", kind="info"))
        return

    table = Table(
        box=None,
        pad_edge=False,
        padding=design.PADDING_DEFAULT,
    )
    for col in ("Server", "URI", "Name", "MIME Type"):
        table.add_column(col, style=design.MUTED)

    for res in all_resources:
        table.add_row(
            res.server_name,
            res.uri[:36],
            res.name[:22],
            res.mime_type or "—",
        )

    repl.console.print(ui.header("MCP Resources"))
    repl.console.print(ui.rule())
    repl.console.print(table)
    repl.console.print(f"\n[dim]{len(all_resources)} resource(s) available.[/dim]")
