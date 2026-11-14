"""Plugin management slash command handlers: /plugin + plugin command registration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL

_log = logging.getLogger("velune.cli.handlers.plugins")


async def cmd_plugin(repl: VeluneREPL, args: str) -> None:
    from rich.table import Table

    parts = args.strip().split(None, 1)
    sub = parts[0].lower() if parts else "list"
    arg = parts[1].strip() if len(parts) > 1 else ""

    if sub in ("list", ""):
        rows = repl._plugin_manager.status()
        if not rows:
            repl.console.print(
                "[dim]No plugins loaded.  Drop a plugin into "
                "[cyan]~/.velune/plugins/[/cyan] or "
                "[cyan].velune/plugins/[/cyan] and run [bold]/plugin reload[/bold].[/dim]"
            )
            return
        tbl = Table(show_header=True, border_style="dim", padding=(0, 1), header_style="bold cyan")
        tbl.add_column("Name", style="cyan", width=18)
        tbl.add_column("Version", width=8)
        tbl.add_column("Cmds", width=5)
        tbl.add_column("Skills", width=6)
        tbl.add_column("Hooks", width=6)
        tbl.add_column("MCP", width=5)
        tbl.add_column("Status", width=10)
        tbl.add_column("Description")
        for r in rows:
            status = "[green]enabled[/green]" if r["enabled"] else "[red]disabled[/red]"
            tbl.add_row(
                r["name"],
                r["version"],
                str(r["commands"]),
                str(r["skills"]),
                "[green]yes[/green]" if r["hooks"] else "[dim]no[/dim]",
                "[green]yes[/green]" if r["mcp"] else "[dim]no[/dim]",
                status,
                r["description"],
            )
        repl.console.print(tbl)

    elif sub == "enable":
        if not arg:
            repl.console.print("[yellow]Usage: /plugin enable <name>[/yellow]")
            return
        ok = repl._plugin_manager.enable(arg)
        if ok:
            await _connect_owned(repl, arg)
            _refresh_command_ui(repl)
        repl.console.print(
            f"[green]Plugin '{arg}' enabled.[/green]"
            if ok
            else f"[yellow]Plugin '{arg}' not found.[/yellow]"
        )

    elif sub == "disable":
        if not arg:
            repl.console.print("[yellow]Usage: /plugin disable <name>[/yellow]")
            return
        ok = repl._plugin_manager.disable(arg)  # revokes hooks, commands and MCP servers
        await repl._plugin_manager.flush()  # wait for the server processes to be closed
        _refresh_command_ui(repl)
        repl.console.print(
            f"[yellow]Plugin '{arg}' disabled.[/yellow]"
            if ok
            else f"[yellow]Plugin '{arg}' not found.[/yellow]"
        )

    elif sub == "reload":
        name = arg or None
        # reload revokes what the plugin(s) registered, rescans, and wires what is found
        # again (hooks, MCP servers and commands) into the registries already in use.
        new_plugins = repl._plugin_manager.reload(name)
        await repl._plugin_manager.flush()
        for plugin in new_plugins:
            await _connect_owned(repl, plugin.name)
        _refresh_command_ui(repl)
        label = f"'{name}'" if name else "all"
        repl.console.print(
            f"[green]Reloaded {label} — {len(new_plugins)} plugin(s) active.[/green]"
        )

    elif sub == "show":
        if not arg:
            repl.console.print("[yellow]Usage: /plugin show <name>[/yellow]")
            return
        p = repl._plugin_manager.get_plugin(arg)
        if p is None:
            repl.console.print(f"[yellow]Plugin '{arg}' not found.[/yellow]")
            return
        s = p.summary()
        repl.console.print(
            f"[bold cyan]{s['name']}[/bold cyan] v{s['version']}  {s['description']}"
        )
        repl.console.print(f"  Author : {s['author']}")
        repl.console.print(f"  Root   : {s['root']}")
        repl.console.print(
            f"  Status : {'[green]enabled[/green]' if s['enabled'] else '[red]disabled[/red]'}"
        )
        if p.commands:
            repl.console.print(f"  [bold]Commands ({len(p.commands)}):[/bold]")
            for cmd in p.commands:
                repl.console.print(f"    [cyan]/{cmd.name}[/cyan]  {cmd.description}")
        if p.skills:
            repl.console.print(f"  [bold]Skills ({len(p.skills)}):[/bold]")
            for skill in p.skills:
                triggers = (
                    ", ".join(skill.triggers)
                    if skill.triggers
                    else "(always)"
                    if skill.always
                    else "(none)"
                )
                repl.console.print(f"    [magenta]{skill.name}[/magenta]  triggers: {triggers}")

    else:
        repl.console.print(
            "[yellow]Unknown sub-command.[/yellow]  "
            "Usage: [bold]/plugin[/bold] [list|enable <name>|disable <name>|reload [name]|show <name>]"
        )


def register_plugin_commands(repl: VeluneREPL, plugins) -> None:
    """Inject plugin slash commands into the live REPL registry.

    Each plugin is registered on its own: bad command data in one plugin is reported and
    skipped, and never stops the others (or the hooks and MCP servers wired after this).
    """
    for plugin in plugins:
        plugin_name = getattr(plugin, "name", "")
        try:
            _register_plugin_commands(repl, plugin, plugin_name)
        except (AttributeError, TypeError, ValueError) as exc:
            _log.warning("Could not register commands from plugin '%s': %s", plugin_name, exc)
    _refresh_command_ui(repl)


def _register_plugin_commands(repl: VeluneREPL, plugin, plugin_name: str) -> None:
    from velune.cli.slash_commands import SlashCommand

    for cmd in plugin.commands:
        plugin_root = plugin.root

        def _make_handler(c=cmd, root=plugin_root, owner=plugin_name):
            async def _handler(args: str) -> None:
                # Checked when the command runs, not only when it was registered: a
                # reference kept by the palette or completer must not outlive the plugin.
                manager = getattr(repl, "_plugin_manager", None)
                if owner and manager is not None and not manager.is_active(owner):
                    repl.console.print(
                        f"[yellow]/{c.name} belongs to plugin '{owner}', which is not "
                        "active.[/yellow]"
                    )
                    return
                rendered = c.render(args, root)
                repl.console.print(
                    f"[dim]Plugin command [bold]/{c.name}[/bold] → sending to model[/dim]"
                )
                await repl._handle_prompt(rendered)

            return _handler

        # The registry refuses a built-in or reserved name (and an alias of one) and
        # a name another plugin already holds; it logs why, so nothing to do here.
        repl._registry.register(
            SlashCommand(
                name=cmd.name,
                aliases=list(cmd.aliases),
                description=f"{cmd.description}  {cmd.help_label}",
                usage=cmd.usage,
                handler=_make_handler(),
                category="Tools",
                owner=plugin_name,
            )
        )


def _refresh_command_ui(repl: VeluneREPL) -> None:
    """Tell the completer and the palette what the registry holds now."""
    if repl._completer is not None:
        from velune.cli.autocomplete import CommandEntry

        entries = [
            CommandEntry(
                name=c.name,
                description=c.description,
                category=c.category,
                aliases=tuple(c.aliases),
            )
            for c in repl._registry.all_unique()
            if not c.hidden
        ]
        repl._completer.set_commands(entries)
    if repl._command_palette is not None:
        repl._command_palette.set_commands(repl._registry.all_unique())


async def _connect_owned(repl: VeluneREPL, owner: str) -> None:
    """Connect the MCP servers a plugin has registered but that are not connected yet."""
    registry = repl._mcp_registry
    for name, entry in registry.owned_entries(owner).items():
        if not entry.is_connected:
            await registry.connect(name)


async def load_and_register_plugins(repl: VeluneREPL, *, workspace_trusted: bool = False) -> None:
    """Load plugins and wire their commands/hooks/MCP servers into the REPL.

    Called once from ``VeluneREPL.run()`` at startup, after the workspace-trust decision.
    *workspace_trusted* is that decision: workspace plugins (``<workspace>/.velune/plugins``)
    are only loaded and wired when it is true. It defaults to false, so a caller that forgets
    to pass it fails closed; user-level plugins load either way.

    A failure here (a bad plugin, a broken import) must never be fatal to the session, but
    it also must never be invisible: it's logged at WARNING (the app's default log
    threshold) and printed to the console, not swallowed at DEBUG.
    """
    try:
        repl._plugin_manager.set_workspace_trusted(workspace_trusted)
        # Lets the manager take plugin commands back out again (disable, reload) and put them
        # back (enable) without knowing anything about the REPL.
        repl._plugin_manager.attach_commands(
            repl._registry, lambda plugins: register_plugin_commands(repl, plugins)
        )
        new_plugins = repl._plugin_manager.load()
        if new_plugins:
            # Three independent steps: a failure in one must not stop the others, or one
            # plugin's bad data could keep another plugin's hooks from being wired.
            register_plugin_commands(repl, new_plugins)
            repl._plugin_manager.wire_hooks(repl._hook_dispatcher)
            repl._plugin_manager.wire_mcp(repl._mcp_registry)
    except Exception as exc:
        from velune.cli import design

        _log.warning("Plugin load error (non-fatal): %s", exc)
        repl.console.print(
            f"[{design.WARN}]Plugin load failed:[/{design.WARN}] {exc}  "
            f"[{design.MUTED}](plugins disabled for this session; run /plugin reload to retry)[/{design.MUTED}]"
        )
