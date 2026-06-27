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
        repl.console.print(
            f"[green]Plugin '{arg}' enabled.[/green]"
            if ok
            else f"[yellow]Plugin '{arg}' not found.[/yellow]"
        )

    elif sub == "disable":
        if not arg:
            repl.console.print("[yellow]Usage: /plugin disable <name>[/yellow]")
            return
        ok = repl._plugin_manager.disable(arg)
        repl.console.print(
            f"[yellow]Plugin '{arg}' disabled.[/yellow]"
            if ok
            else f"[yellow]Plugin '{arg}' not found.[/yellow]"
        )

    elif sub == "reload":
        name = arg or None
        new_plugins = repl._plugin_manager.reload(name)
        label = f"'{name}'" if name else "all"
        repl.console.print(
            f"[green]Reloaded {label} — {len(new_plugins)} plugin(s) active.[/green]"
        )
        if new_plugins:
            register_plugin_commands(repl, new_plugins)

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
    """Inject plugin slash commands into the live REPL registry."""
    from velune.cli.slash_registry import SlashCommand

    for plugin in plugins:
        for cmd in plugin.commands:
            plugin_root = plugin.root

            def _make_handler(c=cmd, root=plugin_root):
                async def _handler(args: str) -> None:
                    rendered = c.render(args, root)
                    repl.console.print(
                        f"[dim]Plugin command [bold]/{c.name}[/bold] → sending to model[/dim]"
                    )
                    await repl._handle_prompt(rendered)

                return _handler

            repl._registry.register(
                SlashCommand(
                    name=cmd.name,
                    aliases=cmd.aliases,
                    description=f"{cmd.description}  {cmd.help_label}",
                    usage=cmd.usage,
                    handler=_make_handler(),
                    category="Tools",
                )
            )
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
