"""Session-level slash command handlers: /help /exit /clear /new /history /stats."""

from __future__ import annotations

import logging
import time as _time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL

_log = logging.getLogger("velune.cli.handlers.session")


async def cmd_help(repl: VeluneREPL, args: str) -> None:
    from rich.table import Table

    from velune.cli.autocomplete import CATEGORY_ORDER

    show_hidden = any(tok in ("--all", "-a", "all") for tok in args.split())

    grouped: dict[str, list] = {}
    for cmd in repl._registry.all_unique():
        if cmd.hidden and not show_hidden:
            continue
        grouped.setdefault(cmd.category, []).append(cmd)

    ordered = [c for c in CATEGORY_ORDER if c in grouped]
    ordered += sorted(c for c in grouped if c not in CATEGORY_ORDER)

    for category in ordered:
        table = Table(
            show_header=False,
            border_style="dim",
            padding=(0, 1),
            title=f"[bold cyan]{category}[/bold cyan]",
            title_justify="left",
        )
        table.add_column("Command", style="cyan", width=16)
        table.add_column("Aliases", style="dim white", width=14)
        table.add_column("Description")
        for cmd in sorted(grouped[category], key=lambda c: c.name):
            aliases = ", ".join(f"/{a}" for a in cmd.aliases) if cmd.aliases else ""
            name = f"/{cmd.name}" + (" [dim](dev)[/dim]" if cmd.hidden else "")
            table.add_row(name, aliases, cmd.description)
        repl.console.print(table)

    tips = (
        "[dim]Tip: press [bold]Tab[/bold] to fuzzy-complete any command · "
        "[bold]/help --all[/bold] shows developer commands · "
        "type [bold]/<cmd>[/bold] with no args for its usage.[/dim]\n"
        "[dim]Troubleshooting: models not showing? → [bold]/model discover[/bold] "
        "or [bold]/model locate[/bold] (custom drive). "
        "Something stuck warming up? → [bold]/doctor[/bold].[/dim]"
    )
    repl.console.print(tips)


async def cmd_exit(repl: VeluneREPL, args: str) -> None:
    repl._exit_requested = True
    raise SystemExit(0)


async def cmd_clear(repl: VeluneREPL, args: str) -> None:
    print("\033c", end="", flush=True)
    repl.console.print(
        "[dim]Screen cleared — conversation context preserved. "
        "Use /new for a fresh session.[/dim]"
    )


async def cmd_new(repl: VeluneREPL, args: str) -> None:
    """Start an isolated conversation session inside the same workspace."""
    archived_note = ""
    try:
        has_exchange = any(m.get("role") == "assistant" for m in repl._conversation)
        if has_exchange:
            workspace = str(repl.container.get("runtime.workspace") or "")
            from velune.cli.sessions import auto_title

            meta = repl._session_store.save(
                repl._conversation,
                workspace=workspace,
                model_id=repl.active_model.model_id if repl.active_model else "unknown",
                mode=repl._mode_manager.current.value,
                title=args.strip() or auto_title(repl._conversation),
                total_tokens=repl.session_tokens,
            )
            archived_note = f"  [dim]previous saved as[/dim] [cyan]{meta.title}[/cyan]"
    except Exception as exc:
        _log.warning("Could not archive previous session: %s", exc)

    await repl._end_episodic_session()
    repl._conversation = []
    repl.session_tokens = 0
    repl.session_cost = 0.0
    repl._context_tracker.update(repl._conversation)
    await repl._start_episodic_session()
    repl.console.print(
        f"[green]New session started[/green] — project memory preserved.{archived_note}"
    )


async def cmd_history(repl: VeluneREPL, args: str) -> None:
    """Show REPL command execution history."""
    if not repl._history_file.exists():
        repl.console.print("[dim]No command history found.[/dim]")
        return

    try:
        lines = repl._history_file.read_text(encoding="utf-8").splitlines()
        cmds = [line[1:] for line in lines if line.startswith("+")]

        if not cmds:
            repl.console.print("[dim]No command history found.[/dim]")
            return

        last_n = cmds[-25:]
        repl.console.print("\n[bold cyan]REPL Command History (last 25):[/bold cyan]")
        for i, cmd in enumerate(last_n, len(cmds) - len(last_n) + 1):
            repl.console.print(f"  [dim]{i:3d}[/dim]  {cmd}")
        repl.console.print()
    except Exception as e:
        repl.console.print(f"[red]Failed to read history: {e}[/red]")


async def cmd_stats(repl: VeluneREPL, args: str) -> None:
    """Show session statistics — tokens, cost, turns, uptime, approval mode."""
    from rich.panel import Panel
    from rich.table import Table

    elapsed = _time.monotonic() - repl._session_start_time
    hours, remainder = divmod(int(elapsed), 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime = (
        f"{hours}h {minutes}m {seconds}s"
        if hours
        else f"{minutes}m {seconds}s"
        if minutes
        else f"{seconds}s"
    )

    turns = len(repl._conversation)
    user_turns = sum(1 for m in repl._conversation if m.get("role") == "user")

    table = Table(show_header=False, border_style="dim", padding=(0, 2))
    table.add_column("Metric", style="dim", width=22)
    table.add_column("Value", style="white")

    table.add_row("Session uptime", uptime)
    table.add_row("Conversation turns", str(turns))
    table.add_row("User messages", str(user_turns))
    table.add_row("Total tokens", f"{repl.session_tokens:,}")
    table.add_row(
        "Estimated cost",
        f"${repl.session_cost:.4f}" if repl.session_cost > 0 else "—",
    )
    table.add_row("Tool calls", str(repl._tool_call_count))
    table.add_row(
        "Active model",
        repl.active_model.model_id if repl.active_model else "none",
    )
    table.add_row("Approval mode", repl._approval_mode.value)
    table.add_row("Session mode", repl._mode_manager.current.value.upper())

    repl.console.print(
        Panel(
            table,
            title="[bold cyan]Session Statistics[/bold cyan]",
            border_style="cyan",
            padding=(0, 1),
        )
    )
