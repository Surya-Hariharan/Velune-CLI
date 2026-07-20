"""Slash-command handlers for /backup, /restore, /recover.

Thin wrappers over :mod:`velune.recovery.archive` and the session store so the
REPL surface stays in lockstep with the top-level CLI commands.
"""

from __future__ import annotations

import os
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from velune.cli import design
from velune.recovery import (
    SUBSYSTEMS,
    archive_has_encrypted_secrets,
    create_backup,
    restore_backup,
)

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL


def _split_args(args: str) -> list[str]:
    """Split slash-command arguments, preserving Windows-style paths.

    ``shlex.split`` defaults to POSIX mode, where backslash is an escape
    character — this silently deletes the backslashes in paths like
    ``C:\\Users\\...``. Fall back to non-POSIX splitting on Windows, which
    leaves backslashes intact, then strip any surrounding quotes that mode
    doesn't remove on its own.
    """
    if not args:
        return []
    tokens = shlex.split(args, posix=(os.name != "nt"))
    if os.name == "nt":
        tokens = [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t for t in tokens]
    return tokens


def _parse_include(tokens: list[str]) -> set[str] | None:
    """Pull a ``--include a,b`` filter out of *tokens*, validating names.

    Raises ``ValueError`` on an unknown subsystem so callers abort rather than
    silently widening the operation to every subsystem.
    """
    for i, tok in enumerate(tokens):
        if tok in ("--include", "-i") and i + 1 < len(tokens):
            names = {n.strip() for n in tokens[i + 1].split(",") if n.strip()}
            unknown = names - set(SUBSYSTEMS)
            if unknown:
                raise ValueError(f"Unknown subsystem(s): {', '.join(sorted(unknown))}")
            return names
    return None


async def cmd_backup(repl: VeluneREPL, args: str) -> None:
    """/backup [path] [--include a,b] [--with-secrets]"""
    console = repl.console
    tokens = _split_args(args)
    with_secrets = "--with-secrets" in tokens
    try:
        include = _parse_include(tokens)
    except ValueError as exc:
        console.print(f"[{design.DANGER}]{exc}[/{design.DANGER}]")
        return
    positional = [t for t in tokens if not t.startswith("-")]
    # Drop the value that followed --include, if any.
    if include is not None and positional:
        inc_raw = next(
            (tokens[i + 1] for i, t in enumerate(tokens) if t in ("--include", "-i")), ""
        )
        positional = [p for p in positional if p != inc_raw]

    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    dest = Path(positional[0]) if positional else Path.cwd() / f"velune-backup-{date_str}.tar.gz"

    passphrase = None
    if with_secrets:
        passphrase = console.input("Passphrase to encrypt provider API keys: ", password=True)
        confirm = console.input("Confirm passphrase: ", password=True)
        if passphrase != confirm:
            console.print(f"[{design.DANGER}]Passphrases did not match.[/{design.DANGER}]")
            return

    workspace = Path(str(repl.container.get("runtime.workspace") or Path.cwd()))
    console.print(f"[{design.MUTED}]Building backup...[/{design.MUTED}]")
    result = create_backup(
        dest,
        include=include,
        with_secrets=with_secrets,
        secrets_passphrase=passphrase,
        workspace=workspace,
    )

    console.print(f"[{design.OK}]Backup written:[/{design.OK}] {result.path}")
    console.print(f"[{design.MUTED}]Size: {result.size_bytes / 1024:.1f} KB[/{design.MUTED}]")
    if result.with_secrets and "providers" in result.subsystems:
        console.print(
            f"[{design.WARN}]Provider API keys are encrypted with your passphrase — it is "
            f"not stored anywhere, so keep it safe.[/{design.WARN}]"
        )


async def cmd_restore(repl: VeluneREPL, args: str) -> None:
    """/restore <archive> [--include a,b] [--overwrite] [--dry-run]"""
    console = repl.console
    tokens = _split_args(args)
    if not tokens:
        console.print(
            f"[{design.WARN}]Usage:[/{design.WARN}] /restore <archive> [--dry-run] [--overwrite]"
        )
        return

    overwrite = "--overwrite" in tokens
    dry_run = "--dry-run" in tokens
    try:
        include = _parse_include(tokens)
    except ValueError as exc:
        console.print(f"[{design.DANGER}]{exc}[/{design.DANGER}]")
        return
    inc_raw = next((tokens[i + 1] for i, t in enumerate(tokens) if t in ("--include", "-i")), "")
    positional = [t for t in tokens if not t.startswith("-") and t != inc_raw]
    if not positional:
        console.print(
            f"[{design.WARN}]Provide the archive path: /restore <archive>[/{design.WARN}]"
        )
        return

    src = Path(positional[0])
    workspace = Path(str(repl.container.get("runtime.workspace") or Path.cwd()))

    passphrase = None
    if (include is None or "providers" in include) and archive_has_encrypted_secrets(src):
        entered = console.input(
            "Passphrase to decrypt provider API keys (blank to skip restoring them): ",
            password=True,
        )
        passphrase = entered or None

    try:
        result = restore_backup(
            src,
            include=include,
            overwrite=overwrite,
            dry_run=dry_run,
            workspace=workspace,
            secrets_passphrase=passphrase,
        )
    except (ValueError, FileNotFoundError) as exc:
        console.print(f"[{design.DANGER}]{exc}[/{design.DANGER}]")
        return

    label = "Would restore" if dry_run else "Restored"
    any_action = False
    for name in SUBSYSTEMS:
        actions = result.restored.get(name)
        if actions:
            any_action = True
            console.print(f"[{design.OK}]{label} {name}:[/{design.OK}] {', '.join(actions)}")
        skipped = result.skipped.get(name)
        if skipped:
            console.print(f"[{design.MUTED}]Skipped {name}:[/{design.MUTED}] {', '.join(skipped)}")
    if not any_action:
        console.print(f"[{design.MUTED}]Nothing restored.[/{design.MUTED}]")


async def cmd_recover(repl: VeluneREPL, args: str) -> None:
    """/recover [id] [--all] [--all-workspaces]"""
    console = repl.console
    store = repl._session_store
    tokens = _split_args(args)
    recover_all = "--all" in tokens or "-a" in tokens
    all_workspaces = "--all-workspaces" in tokens
    positional = [t for t in tokens if not t.startswith("-")]

    # Scoped to this workspace by default — a crash in another project
    # shouldn't surface as something to recover here. `--all-workspaces`
    # opts back into the old global view.
    workspace = None if all_workspaces else repl.container.get("runtime.workspace")
    orphans = store.list_orphaned_autosaves(workspace=workspace)

    if recover_all:
        if not orphans:
            console.print(f"[{design.MUTED}]No unsaved sessions to recover.[/{design.MUTED}]")
            return
        for meta in orphans:
            saved = store.recover_autosave(meta.id)
            if saved:
                console.print(f"[{design.OK}]Recovered[/{design.OK}] {saved.id} — {saved.title}")
        return

    if positional:
        saved = store.recover_autosave(positional[0])
        if saved is None:
            console.print(
                f"[{design.DANGER}]No orphaned autosave '{positional[0]}' found.[/{design.DANGER}]"
            )
            return
        console.print(
            f"[{design.OK}]Recovered session [bold]{saved.id}[/bold] — {saved.title}[/{design.OK}]"
        )
        return

    if not orphans:
        console.print(f"[{design.OK}]No unsaved sessions — nothing to recover.[/{design.OK}]")
        return

    from rich.box import ROUNDED
    from rich.table import Table

    table = Table(box=ROUNDED, border_style=design.FAINT, expand=False)
    table.add_column("ID", style=design.ACCENT, no_wrap=True)
    table.add_column("Title", style=design.INFO)
    table.add_column("Updated", style=design.MUTED, no_wrap=True)
    table.add_column("Turns", justify="right", style=design.MUTED)
    for m in orphans:
        table.add_row(m.id, m.title, m.updated_at[:16].replace("T", " "), str(m.turn_count))
    console.print(table)
    if workspace:
        console.print(
            f"[{design.MUTED}]Showing this workspace only — "
            f"[/][bold]/recover --all-workspaces[/bold][{design.MUTED}] for every project.[/]"
        )
    console.print(
        f"[{design.MUTED}]Recover one: [/]/recover <id>   [{design.MUTED}]all: [/]/recover --all"
    )
