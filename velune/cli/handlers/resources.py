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
from velune.resources.base import ResourcePermission, ResourceState

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

    resource_id, _, argument = resource_id.strip().partition(" ")
    if not resource_id:
        repl.console.print(
            "[yellow]Usage: /resource configure <postgres|mysql|supabase|http <reference>>[/yellow]"
        )
        return
    if resource_id == "http":
        await _configure_http(repl, argument.strip())
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

    from velune.resources.connections import CONNECTION_NAME

    # An optional word names the connection: /resource configure postgres staging
    # Options (never prompts, so the interactive questions stay exactly the same):
    #   --env <label>   --read-only   --allow-remote [--tls require|verify-ca|verify-full]
    parsed = _parse_configure_options(argument)
    if isinstance(parsed, str):
        repl.console.print(f"[yellow]{parsed}[/yellow]")
        return
    name, options = parsed
    connection = name or resource_id
    if not CONNECTION_NAME.match(connection):
        repl.console.print(
            "[yellow]A connection name is lowercase letters, digits, '.', '_' or '-' "
            "(for example: staging).[/yellow]"
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

    _apply_connection_options(repl, resource_id, config, options)

    from velune.resources.secrets import save_resource_secret

    save_resource_secret(resource_id, connection, config)
    named = "" if connection == resource_id else f" {connection}"
    repl.console.print(
        f"[green]Saved {resource_id}{named} configuration.[/green] "
        f"[dim]Connect with /resource connect {resource_id}; operations name it with "
        f"connection={connection}[/dim]"
    )


_TLS_MODES = ("require", "verify-ca", "verify-full")


def _parse_configure_options(argument: str) -> tuple[str, dict[str, object]] | str:
    """Split ``[name] [--env x] [--read-only] [--allow-remote] [--tls mode]``.

    Returns ``(connection_name_or_empty, options)`` or an error message.
    """
    words = argument.split()
    name = ""
    options: dict[str, object] = {}
    i = 0
    while i < len(words):
        word = words[i]
        if word == "--read-only":
            options["read_only"] = True
        elif word == "--allow-remote":
            options["allow_remote"] = True
        elif word in ("--env", "--tls"):
            if i + 1 >= len(words) or words[i + 1].startswith("--"):
                return f"{word} needs a value."
            options[word[2:]] = words[i + 1]
            i += 1
        elif word.startswith("--"):
            return (
                f"Unknown option {word}. Options: --env <label>, --read-only, "
                "--allow-remote [--tls require|verify-ca|verify-full]."
            )
        elif not name:
            name = word
        else:
            return "Only one connection name is allowed."
        i += 1
    if "tls" in options and options["tls"] not in _TLS_MODES:
        return "--tls must be one of: " + ", ".join(_TLS_MODES) + "."
    return name, options


def _apply_connection_options(
    repl: VeluneREPL, resource_id: str, config: dict, options: dict[str, object]
) -> None:
    """Record the environment label, read-only flag and remote opt-in in the *encrypted*
    record, next to the endpoint they govern, so a repository can never loosen them."""
    if options.get("env"):
        config["environment"] = str(options["env"]).strip().lower()[:30]
    if resource_id == "supabase":
        return
    if options.get("read_only"):
        config["read_only"] = True
    from velune.resources.sql_base import SQLConnector

    host = str(config.get("host") or "localhost")
    if SQLConnector.is_local_host(host):
        return
    if options.get("allow_remote"):
        config["allow_remote"] = True
        config["sslmode"] = str(options.get("tls") or "verify-full")
    else:
        repl.console.print(
            f"[yellow]'{host}' is not a local host, so connecting to it will be refused. Velune "
            "sends a stored password to a remote server only when you allow it and require TLS: "
            f"re-run with [bold]/resource configure {resource_id} <name> --allow-remote "
            "--tls verify-full[/bold].[/yellow]"
        )


_HTTP_FIELDS: dict[str, tuple[tuple[str, str, bool], ...]] = {
    "bearer": (("token", "Token", True),),
    "basic": (("username", "Username", False), ("password", "Password", True)),
    "api_key_header": (("name", "Header name", False), ("value", "API key", True)),
    "api_key_query": (("name", "Query parameter name", False), ("value", "API key", True)),
    "oauth2_client_credentials": (
        ("token_url", "Token URL", False),
        ("client_id", "Client id", False),
        ("client_secret", "Client secret", True),
        ("scope", "Scope (optional)", False),
    ),
    "secret": (("value", "Secret value", True),),
}


async def _configure_http(repl: VeluneREPL, reference: str) -> None:
    """Store an HTTP credential (encrypted) under a reference name.

    The reference is what configuration, collections and requests name; the secret
    itself goes only into the encrypted keystore. A credential is bound to one
    origin and is never sent anywhere else.
    """
    from rich.prompt import Prompt

    from velune.resources.http.auth import CredentialError, save_credential, valid_reference

    if not valid_reference(reference):
        repl.console.print(
            "[yellow]Usage: /resource configure http <reference>  "
            "(lowercase letters, digits, '.', '_' or '-', e.g. aidly-dev-token)[/yellow]"
        )
        return

    def ask(label: str, *, secret: bool = False, default: str = "") -> object:
        return asyncio.to_thread(
            Prompt.ask, f"  {label}", password=secret, default=default, console=repl.console
        )

    repl.console.print(
        f"[dim]Credential '{reference}' — values are encrypted at rest and bound to one origin.[/dim]"
    )
    try:
        kind = str(
            await asyncio.to_thread(
                Prompt.ask,
                "  Type",
                choices=list(_HTTP_FIELDS),
                default="bearer",
                console=repl.console,
            )
        )
        document: dict[str, object] = {"type": kind}
        if kind != "secret":
            document["origin"] = await ask("Origin it is valid for (e.g. https://api.example.com)")
        for key, label, secret in _HTTP_FIELDS[kind]:
            value = await ask(
                label,
                secret=secret,
                default="X-API-Key" if key == "name" and kind == "api_key_header" else "",
            )
            if value:
                document[key] = value
    except (EOFError, KeyboardInterrupt):
        repl.console.print("\n[yellow]Cancelled.[/yellow]")
        return
    try:
        save_credential(reference, document)  # type: ignore[arg-type]
    except CredentialError as exc:
        repl.console.print(f"[red]Not saved: {exc}[/red]")
        return
    repl.console.print(
        f"[green]Saved credential '{reference}'.[/green] "
        f'[dim]Use it as auth = "{reference}" in an environment or request.[/dim]'
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
            ui.notification(f"No connector '{resource_id}' (unknown or disabled).", kind="warning")
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
    for col in ("Action", "Permission", "Scope", "Description"):
        table.add_column(col, style=design.MUTED)
    for cap in caps:
        style = _perm_style.get(cap.permission, "white")
        label = cap.permission.value.upper() + (" ⚠" if cap.destructive else "")
        table.add_row(
            cap.action,
            f"[{style}]{label}[/{style}]",
            cap.scope or f"{resource_id}.{cap.action}",
            cap.description,
        )

    repl.console.print(ui.header(f"{resource_id} capabilities"))
    repl.console.print(ui.rule())
    repl.console.print(table)
    repl.console.print(
        "\n[dim]Actions follow your execution mode: reads always run; MANUAL asks before any "
        "change; PLAN refuses changes; AUTO runs ordinary changes but still asks for "
        "destructive ones and for anything this task did not create.[/dim]"
    )


def make_resource_context_factory(repl: VeluneREPL):
    """Execution context for resource actions: the session's permission gate.

    Every resource action is authorized by the same
    :class:`~velune.permissions.gate.PermissionGate` as the agent's own tools —
    the session's execution mode (PLAN / MANUAL / AUTO), its "always allow"
    grants, its approval prompts and its audit trail. ``/approve block`` needs no
    special case: it selects PLAN mode, which refuses every change.
    """
    from pathlib import Path

    from velune.tools.base.tool import ToolCallContext

    def factory() -> ToolCallContext:
        from velune.cli.handlers.tool_chat import _ToolActivityUI, session_gate

        ui = _ToolActivityUI(repl)
        gate = session_gate(repl, ui)
        return ToolCallContext(
            run_id=getattr(repl, "_session_id", "repl"),
            actor="resource",
            workspace=Path(gate.boundary.workspace_root),
            hook_dispatcher=getattr(repl, "_hook_dispatcher", None),
            session_id=getattr(repl, "_episodic_session_id", None)
            or getattr(repl, "_session_id", ""),
            gate=gate,
        )

    return factory
