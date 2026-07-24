"""Resource connector slash command handlers: ``/resource``.

Surfaces the :class:`velune.resources.manager.ResourceManager` in the REPL:
discovery, connection lifecycle, status, and capability inspection for Docker,
local PostgreSQL/MySQL, and Supabase. Non-read actions triggered through the
manager are gated by the same interactive approver the tool loop uses.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from rich.table import Table

from velune.cli import design
from velune.resources.base import AuthorizationRequest, ResourcePermission, ResourceState

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL

_log = logging.getLogger("velune.cli.handlers.resources")

_STATE_STYLE = {
    ResourceState.CONNECTED: "green",
    ResourceState.CONNECTING: "yellow",
    ResourceState.DISCONNECTED: "dim",
    ResourceState.UNAVAILABLE: "dim",
    ResourceState.ERROR: "red",
}


async def cmd_resource(repl: VeluneREPL, args: str) -> None:
    """Inspect and manage external resource connectors."""
    parts = args.strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else "list"
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub in ("", "list", "status"):
        _show_status(repl)
    elif sub == "discover":
        await _discover(repl)
    elif sub in ("configure", "set", "config"):
        await _configure(repl, rest)
    elif sub == "connect":
        await _connect(repl, rest)
    elif sub == "disconnect":
        await _disconnect(repl, rest)
    elif sub == "info":
        _info(repl, rest)
    else:
        repl.console.print(
            "[yellow]Unknown sub-command. Try: /resource list | discover | "
            "configure <id> | connect <id> | disconnect <id> | status | info <id>[/yellow]"
        )


def _show_status(repl: VeluneREPL) -> None:
    from velune.cli import ui

    rows = repl._resource_manager.all_status()
    if not rows:
        repl.console.print(ui.notification("No resource connectors registered.", kind="info"))
        return

    table = Table(
        box=None,
        pad_edge=False,
        padding=design.PADDING_DEFAULT,
    )
    for col in ("Resource", "State", "Detail", "Info"):
        table.add_column(col, style=design.MUTED)
    for st in rows:
        style = _STATE_STYLE.get(st.state, "dim")
        err = f" ({st.error[:40]})" if st.error else ""
        info_bits = ", ".join(f"{k}={v}" for k, v in st.info.items() if k != "url")
        table.add_row(
            st.display_name,
            f"[{style}]{st.state.value}{err}[/{style}]",
            st.detail or "—",
            info_bits or "—",
        )

    repl.console.print(ui.header("Resources"))
    repl.console.print(ui.rule())
    repl.console.print(table)
    repl.console.print(
        "\n[dim]Sub-commands: /resource discover | configure <id> | connect <id> | "
        "disconnect <id> | info <id>[/dim]"
    )


async def _discover(repl: VeluneREPL) -> None:
    from velune.cli import ui

    repl.console.print("[dim]Scanning environment for resources…[/dim]")
    hints = await repl._resource_manager.discover()
    if not hints:
        repl.console.print(
            ui.notification(
                "No resources detected. Install Docker or configure a database to get started.",
                kind="info",
            )
        )
        return

    table = Table(
        box=None,
        pad_edge=False,
        padding=design.PADDING_DEFAULT,
    )
    for col in ("Resource", "Detected", "Source"):
        table.add_column(col, style=design.MUTED)
    for hint in hints:
        table.add_row(hint.display_name, hint.detail or "—", hint.source or "—")

    repl.console.print(ui.header("Discovered Resources"))
    repl.console.print(ui.rule())
    repl.console.print(table)

    ids = sorted({h.resource_id for h in hints})
    commands = [
        f"[cyan]/resource connect {rid}[/cyan]"
        if rid == "docker"
        else f"[cyan]/resource configure {rid}[/cyan]"
        for rid in ids
    ]
    repl.console.print("\n[dim]Get started:[/dim] " + " ".join(commands))


_SQL_DEFAULT_PORT = {"postgres": 5432, "mysql": 3306}


async def _configure(repl: VeluneREPL, resource_id: str) -> None:
    """Interactively collect and store credentials for a connector.

    Docker needs nothing — ``/resource connect docker`` works with no prior
    setup. PostgreSQL/MySQL/Supabase have no other way to become reachable:
    they refuse to connect until a config exists in the encrypted keystore.
    """
    from rich.prompt import Prompt

    if not resource_id:
        repl.console.print("[yellow]Usage: /resource configure <postgres|mysql|supabase>[/yellow]")
        return
    if resource_id == "docker":
        repl.console.print(
            "[dim]Docker needs no configuration — run "
            "[cyan]/resource connect docker[/cyan] directly.[/dim]"
        )
        return
    if resource_id not in ("postgres", "mysql", "supabase"):
        repl.console.print(
            f"[yellow]'{resource_id}' is not configurable. Try: postgres, mysql, supabase.[/yellow]"
        )
        return

    suggested: dict[str, object] = {}
    try:
        for hint in await repl._resource_manager.discover():
            if hint.resource_id == resource_id and hint.suggested:
                suggested = hint.suggested
                break
    except Exception as exc:
        _log.debug("Pre-fill discovery for '%s' failed: %s", resource_id, exc)

    repl.console.print(f"[dim]Configuring {resource_id} — values are encrypted at rest.[/dim]")
    try:
        if resource_id == "supabase":
            url = await asyncio.to_thread(
                Prompt.ask,
                "  Project URL",
                default=str(suggested.get("url", "")),
                console=repl.console,
            )
            if not url:
                repl.console.print("[yellow]Cancelled — a project URL is required.[/yellow]")
                return
            anon_key = await asyncio.to_thread(
                Prompt.ask, "  Anon key", password=True, console=repl.console
            )
            service_role_key = await asyncio.to_thread(
                Prompt.ask,
                "  Service-role key (optional — enables privileged actions)",
                password=True,
                default="",
                console=repl.console,
            )
            config: dict[str, object] = {"url": url, "anon_key": anon_key}
            if service_role_key:
                config["service_role_key"] = service_role_key
        else:
            default_port = _SQL_DEFAULT_PORT[resource_id]
            host = await asyncio.to_thread(
                Prompt.ask,
                "  Host",
                default=str(suggested.get("host", "localhost")),
                console=repl.console,
            )
            port_str = await asyncio.to_thread(
                Prompt.ask,
                "  Port",
                default=str(suggested.get("port", default_port)),
                console=repl.console,
            )
            database = await asyncio.to_thread(
                Prompt.ask,
                "  Database",
                default=str(suggested.get("database", "")),
                console=repl.console,
            )
            username = await asyncio.to_thread(
                Prompt.ask,
                "  Username",
                default=str(suggested.get("username", "")),
                console=repl.console,
            )
            password = await asyncio.to_thread(
                Prompt.ask, "  Password", password=True, default="", console=repl.console
            )
            config = {
                "host": host,
                "port": int(port_str) if port_str.strip().isdigit() else default_port,
                "database": database,
                "username": username,
                "password": password,
            }
    except (EOFError, KeyboardInterrupt):
        repl.console.print("\n[yellow]Cancelled.[/yellow]")
        return

    from velune.resources.secrets import save_resource_secret

    save_resource_secret(resource_id, resource_id, config)
    repl.console.print(
        f"[green]Saved {resource_id} configuration.[/green] "
        f"[dim]Connect with /resource connect {resource_id}[/dim]"
    )


async def _connect(repl: VeluneREPL, resource_id: str) -> None:
    if not resource_id:
        repl.console.print("[yellow]Usage: /resource connect <id>[/yellow]")
        return
    repl.console.print(f"[dim]Connecting to '{resource_id}'…[/dim]")
    result = await repl._resource_manager.connect(resource_id)
    if result.ok:
        repl.console.print(f"[green]Connected to [bold]{resource_id}[/bold].[/green]")
    else:
        repl.console.print(f"[red]Could not connect to {resource_id}: {result.error}[/red]")


async def _disconnect(repl: VeluneREPL, resource_id: str) -> None:
    if not resource_id:
        repl.console.print("[yellow]Usage: /resource disconnect <id>[/yellow]")
        return
    result = await repl._resource_manager.disconnect(resource_id)
    if result.ok:
        repl.console.print(f"[dim]Disconnected from [bold]{resource_id}[/bold].[/dim]")
    else:
        repl.console.print(f"[red]{result.error}[/red]")


def _info(repl: VeluneREPL, resource_id: str) -> None:
    from velune.cli import ui

    if not resource_id:
        repl.console.print("[yellow]Usage: /resource info <id>[/yellow]")
        return
    caps = repl._resource_manager.capabilities(resource_id)
    if not caps:
        repl.console.print(
            ui.notification(
                f"No connector '{resource_id}' (unknown or disabled).", kind="warning"
            )
        )
        return

    _perm_style = {
        ResourcePermission.READ: "green",
        ResourcePermission.WRITE: "yellow",
        ResourcePermission.EXECUTE: "yellow",
        ResourcePermission.ADMIN: "red",
    }
    table = Table(
        box=None,
        pad_edge=False,
        padding=design.PADDING_DEFAULT,
    )
    for col in ("Action", "Permission", "Description"):
        table.add_column(col, style=design.MUTED)
    for cap in caps:
        style = _perm_style.get(cap.permission, "white")
        label = cap.permission.value.upper() + (" ⚠" if cap.destructive else "")
        table.add_row(cap.action, f"[{style}]{label}[/{style}]", cap.description)

    repl.console.print(ui.header(f"{resource_id} capabilities"))
    repl.console.print(ui.rule())
    repl.console.print(table)
    repl.console.print(
        "\n[dim]READ actions run without prompting; WRITE/EXECUTE/ADMIN require approval.[/dim]"
    )


def make_resource_approver(repl: VeluneREPL):
    """Build the interactive approver the ResourceManager uses for gated actions.

    Mirrors the tool-loop approval UX: READ never reaches here (auto-approved in
    the manager); ``/approve block`` denies without prompting; a destructive
    ADMIN action demands a stronger confirmation.
    """
    from velune.tools.safety import ApprovalMode

    async def approver(request: AuthorizationRequest) -> bool:
        if repl._approval_mode is ApprovalMode.BLOCK:
            repl.console.print(
                f"[red]✗ {request.resource_id}.{request.action} denied (approval mode: block)[/red]"
            )
            return False
        return await _prompt(repl, request)

    return approver


async def _prompt(repl: VeluneREPL, request: AuthorizationRequest) -> bool:
    from rich.panel import Panel
    from rich.prompt import Prompt

    tier = request.permission.value.upper()
    warn = (
        "  [red bold]This is a destructive operation.[/red bold]\n" if request.destructive else ""
    )
    body = (
        f"[bold]{request.display_name}[/bold] → [cyan]{request.action}[/cyan] "
        f"([yellow]{tier}[/yellow])\n{warn}"
        f"[dim]{request.preview}[/dim]"
    )
    repl.console.print(
        Panel(
            body,
            title="[yellow]Resource action approval[/yellow]",
            border_style="yellow",
            padding=(0, 2),
        )
    )
    try:
        answer = await asyncio.to_thread(
            Prompt.ask,
            "  Allow? [bold]y[/bold]es / [bold]n[/bold]o",
            choices=["y", "n"],
            default="n",
            console=repl.console,
        )
    except (EOFError, KeyboardInterrupt, Exception) as exc:
        _log.debug("Resource approval prompt unavailable (%s); denying %s", exc, request.action)
        return False
    return answer == "y"
