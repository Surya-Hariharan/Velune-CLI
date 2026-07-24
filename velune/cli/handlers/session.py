"""Session-level slash command handlers: /help /exit /clear /new /history /stats."""

from __future__ import annotations

import logging
import time as _time
from typing import TYPE_CHECKING

from rich.table import Table

from velune.cli import design

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL

_log = logging.getLogger("velune.cli.handlers.session")


async def cmd_help(repl: VeluneREPL, args: str) -> None:
    from velune.cli.autocomplete import CATEGORY_ORDER, fuzzy_score

    tokens = args.split()
    show_hidden = any(tok in ("--all", "-a", "all") for tok in tokens)
    query = " ".join(tok for tok in tokens if tok not in ("--all", "-a", "all")).strip()

    grouped: dict[str, list] = {}
    for cmd in repl._registry.all_unique():
        if cmd.hidden and not show_hidden:
            continue
        grouped.setdefault(cmd.category, []).append(cmd)

    if query:
        # Search across name, aliases, description, and search_terms — the
        # same corpus the command palette fuzzy-matches against — so
        # "/help <word>" finds a command by what it does, not just its name.
        def _best_score(cmd) -> int:
            haystacks = [cmd.name, cmd.description, *cmd.aliases, *cmd.search_terms]
            return max((fuzzy_score(query, h) for h in haystacks), default=0)

        matches = [(cmd, _best_score(cmd)) for cmds in grouped.values() for cmd in cmds]
        matches = sorted((m for m in matches if m[1] > 0), key=lambda m: -m[1])

        if not matches:
            repl.console.print(f"[dim]No commands match {query!r}.[/dim]")
            return

        table = Table(
            box=None,
            pad_edge=False,
            padding=design.PADDING_DEFAULT,
            title=f"Search: {query!r}",
            title_style=f"bold {design.ACCENT}",
            title_justify="left",
        )
        for col in ("Command", "Aliases", "Description"):
            table.add_column(col, style=design.MUTED)
        for cmd, _score in matches:
            aliases = ", ".join(f"/{a}" for a in cmd.aliases) if cmd.aliases else ""
            name = f"[cyan]/{cmd.name}[/cyan]" + (" [dim](dev)[/dim]" if cmd.hidden else "")
            table.add_row(name, f"[dim white]{aliases}[/dim white]", cmd.description)
        repl.console.print(table)
        repl.console.print()
        repl.console.print(f"[dim]{len(matches)} match(es)  ·  /help with no args to see everything[/dim]")
        return

    ordered = [c for c in CATEGORY_ORDER if c in grouped]
    ordered += sorted(c for c in grouped if c not in CATEGORY_ORDER)

    for category in ordered:
        table = Table(
            box=None,
            pad_edge=False,
            padding=design.PADDING_DEFAULT,
            title=category,
            title_style=f"bold {design.ACCENT}",
            title_justify="left",
        )
        for col in ("Command", "Aliases", "Description"):
            table.add_column(col, style=design.MUTED)
        for cmd in sorted(grouped[category], key=lambda c: c.name):
            aliases = ", ".join(f"/{a}" for a in cmd.aliases) if cmd.aliases else ""
            name = f"[cyan]/{cmd.name}[/cyan]" + (" [dim](dev)[/dim]" if cmd.hidden else "")
            table.add_row(name, f"[dim white]{aliases}[/dim white]", cmd.description)
        repl.console.print(table)
        repl.console.print()

    repl.console.print(
        "[dim]"
        "  [bold]Tab[/bold] autocomplete"
        "  ·  type [bold]/[/bold] to open command palette"
        "  ·  [bold]@file.py[/bold] to mention files in prompts"
        "  ·  [bold]/help --all[/bold] for dev commands"
        "  ·  [bold]/help <word>[/bold] to search"
        "[/dim]"
    )


async def cmd_exit(repl: VeluneREPL, args: str) -> None:
    repl._exit_requested = True
    raise SystemExit(0)


async def cmd_clear(repl: VeluneREPL, args: str) -> None:
    from velune.cli import ui as cli_ui

    fullscreen_ui = getattr(repl, "_fullscreen_ui", None)
    if fullscreen_ui is not None:
        fullscreen_ui.clear()
    else:
        print("\033c", end="", flush=True)
    repl.console.print(
        cli_ui.notification("Screen cleared — conversation context preserved.", kind="success")
    )


async def cmd_new(repl: VeluneREPL, args: str) -> None:
    """Start an isolated conversation session inside the same workspace."""
    from velune.cli import ui

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
            archived_note = f" (Previous saved as {meta.title})"
    except Exception as exc:
        _log.warning("Could not archive previous session: %s", exc)

    await repl._end_episodic_session()
    repl._conversation = []
    repl.session_tokens = 0
    repl.session_cost = 0.0
    repl._context_tracker.update(repl._conversation)
    await repl._start_episodic_session()
    repl.console.print(
        ui.notification(
            f"New session started — project memory preserved.{archived_note}",
            kind="success",
        )
    )


async def cmd_fork(repl: VeluneREPL, args: str) -> None:
    """Start a new session pre-populated with a prefix of the current one.

    ``/fork`` (no arg) branches at the current end — a checkpoint before
    trying something risky. ``/fork <turn>`` branches earlier, dropping
    everything from that point on. Either way the *original* conversation is
    archived intact under its own id first — forking never mutates it, it
    only starts a new session next to it. This is deliberately just "a new
    ordinary session seeded from a slice," not a tree/parent-pointer model:
    there's no session schema change, and the two sessions are otherwise
    unrelated once forked.
    """
    import uuid

    from velune.cli import ui

    if not repl._conversation:
        repl.console.print(
            ui.notification("Nothing to fork — the conversation is empty.", kind="info")
        )
        return

    turn_index = len(repl._conversation)
    if args.strip():
        try:
            turn_index = int(args.strip())
        except ValueError:
            repl.console.print(
                ui.notification(f"'{args.strip()}' is not a valid turn number.", kind="error")
            )
            return
    turn_index = max(0, min(turn_index, len(repl._conversation)))

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
                title=auto_title(repl._conversation),
                total_tokens=repl.session_tokens,
            )
            archived_note = f" Original preserved as {meta.title!r}."
    except Exception as exc:
        _log.warning("Could not archive session before forking: %s", exc)

    await repl._end_episodic_session()
    repl._conversation = repl._conversation[:turn_index]
    repl._session_id = uuid.uuid4().hex[:8]
    repl.session_tokens = 0
    repl.session_cost = 0.0
    repl._context_tracker.update(repl._conversation)
    await repl._start_episodic_session()

    repl.console.print(
        ui.notification(
            f"Forked at turn {turn_index} ({len(repl._conversation)} messages carried over)."
            f"{archived_note}",
            kind="success",
        )
    )


async def cmd_history(repl: VeluneREPL, args: str) -> None:
    """Show REPL command execution history."""
    from velune.cli import ui

    if not repl._history_file.exists():
        repl.console.print(ui.notification("No command history found.", kind="info"))
        return

    try:
        lines = repl._history_file.read_text(encoding="utf-8").splitlines()
        cmds = [line[1:] for line in lines if line.startswith("+")]

        if not cmds:
            repl.console.print(ui.notification("No command history found.", kind="info"))
            return

        last_n = cmds[-25:]
        repl.console.print(ui.header("REPL Command History", "Last 25 executed commands"))
        repl.console.print(ui.rule())
        for i, cmd in enumerate(last_n, len(cmds) - len(last_n) + 1):
            repl.console.print(f"  [dim]{i:3d}[/dim]  {cmd}")
        repl.console.print()
    except Exception as e:
        repl.console.print(ui.notification(f"Failed to read history: {e}", kind="error"))


async def cmd_stats(repl: VeluneREPL, args: str) -> None:
    """Show session statistics — tokens, cost, turns, uptime, approval mode."""
    from velune.cli import ui

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

    repl.console.print(ui.header("Session Statistics"))
    repl.console.print(ui.rule())

    table = Table(
        box=None,
        pad_edge=False,
        padding=design.PADDING_DEFAULT,
    )
    for col in ("Metric", "Value"):
        table.add_column(col, style=design.MUTED)
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

    repl.console.print(table)
    repl.console.print()
