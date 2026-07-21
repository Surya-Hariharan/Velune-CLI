"""Execution trace slash command handler: /trace.

Same persisted, secret-redacted trace log the top-level `velune trace`
command reads at the shell — this is a REPL-native window onto the current
workspace's recent events so a user doesn't have to exit the session to
check what Velune actually did. Only the "recent" view is offered here;
`velune trace live` (tailing a separate running session) stays a shell-only
command since it's an open-ended polling loop that doesn't fit a single
slash-command turn.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL

_log = logging.getLogger("velune.cli.handlers.trace")

_DEFAULT_LIMIT = 20


async def cmd_trace(repl: VeluneREPL, args: str) -> None:
    """Show recent execution trace events for this workspace.

    Usage: /trace [limit] [type-filter]
    """
    from velune.cli.commands.trace import _render_table
    from velune.observability.trace_sink import trace_log_for_workspace

    tokens = args.split()
    limit = _DEFAULT_LIMIT
    if tokens and tokens[0].isdigit():
        limit = max(1, int(tokens[0]))
        tokens = tokens[1:]
    type_filter = tokens[0] if tokens else ""

    workspace = Path(repl.container.get("runtime.workspace") or ".")
    log = trace_log_for_workspace(workspace)
    records = log.read_recent(limit=limit, type_filter=type_filter or None)

    if not records:
        repl.console.print("[dim]No trace events recorded yet for this workspace.[/dim]")
        return

    _render_table(repl.console, records, title=f"Recent execution trace ({len(records)} events)")
    repl.console.print(f"[dim]{log.count()} total events stored · {log.path}[/dim]")
