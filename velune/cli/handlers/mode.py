"""Mode switch slash command handlers: /fast /max /normal /mode.

Deduplicates /fast and /max which shared identical structure —
now both delegate to a single _set_session_mode helper.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL

_log = logging.getLogger("velune.cli.handlers.mode")


async def _set_session_mode(repl: VeluneREPL, mode_enum, label: str, color: str) -> None:
    """Shared body for /fast, /max, /normal — avoids copy-paste."""
    from velune.cli.model_selector import ModeAwareModelSelector

    config = repl._mode_manager.set_mode(mode_enum)
    selector = ModeAwareModelSelector(
        repl.container.get_optional("runtime.model_registry"),
        repl.container.get_optional("runtime.provider_registry"),
        runtime_profile=repl._runtime_profile,
    )
    auto_model = selector.select_for_mode(config, repl.active_model)
    if auto_model:
        repl.active_model = auto_model

    extra = ""
    if hasattr(config, "retrieval_depth") and config.retrieval_depth:
        extra = f" · Retrieval depth: {config.retrieval_depth}"
    if hasattr(config, "council_tier"):
        council_info = f"Council: {config.council_tier}"
    else:
        council_info = ""

    repl.console.print(
        f"[{color}]{label}[/{color}] — {config.description}\n"
        f"[dim]Model: {repl.active_model.model_id if repl.active_model else 'none'} · "
        f"Context cap: {config.max_context_tokens:,} tokens · "
        f"{council_info}{extra}[/dim]"
    )


async def cmd_optimus(repl: VeluneREPL, args: str) -> None:
    from velune.cli.modes import SessionMode

    await _set_session_mode(repl, SessionMode.OPTIMUS, "OPTIMUS MODE", "yellow")


async def cmd_godly(repl: VeluneREPL, args: str) -> None:
    from velune.cli.modes import SessionMode

    await _set_session_mode(repl, SessionMode.GODLY, "GODLY MODE", "magenta")


async def cmd_normal(repl: VeluneREPL, args: str) -> None:
    from velune.cli.modes import SessionMode

    config = repl._mode_manager.set_mode(SessionMode.NORMAL)
    repl.console.print(f"[cyan]NORMAL MODE[/cyan] — {config.description}")


async def cmd_mode(repl: VeluneREPL, args: str) -> None:
    """/mode doubles as a switcher: fast|max|normal change mode, or show current."""
    sub = args.strip().lower()
    if sub in ("fast", "optimus", "speed"):
        return await cmd_optimus(repl, "")
    if sub in ("max", "godly", "full"):
        return await cmd_godly(repl, "")
    if sub in ("normal", "reset", "balanced"):
        return await cmd_normal(repl, "")
    if sub not in ("", "status", "show"):
        repl.console.print(
            f"[yellow]Unknown /mode subcommand: {sub}[/yellow]  "
            "[dim]fast | max | normal | status[/dim]"
        )
        return

    from rich.table import Table

    config = repl._mode_manager.config
    table = Table(border_style="dim", padding=(0, 1), show_header=False)
    table.add_column("Setting", style="dim", width=22)
    table.add_column("Value", style="white")
    table.add_row("Active mode", f"[bold]{config.mode.value.upper()}[/bold]")
    if repl._runtime_profile:
        table.add_row(
            "Runtime profile",
            f"{repl._runtime_profile.label} [dim]— {repl._runtime_profile.description}[/dim]",
        )
    table.add_row("Description", config.description)
    table.add_row("Council tier", config.council_tier)
    table.add_row("Max context", f"{config.max_context_tokens:,} tokens")
    table.add_row("Compression", "on" if config.context_compression else "off")
    table.add_row("Retrieval depth", str(config.retrieval_depth))
    table.add_row("Critics", "disabled" if config.disable_critics else "enabled")
    table.add_row("Current model", repl.active_model.model_id if repl.active_model else "none")
    repl.console.print(table)
