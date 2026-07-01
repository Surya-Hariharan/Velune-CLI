"""Unified backup / restore commands — velune backup / restore.

Snapshots every Velune subsystem (sessions, config, providers, memory, trust)
into one portable ``.tar.gz`` and restores it onto the current machine. The
heavy lifting lives in :mod:`velune.recovery.archive`; this module is the thin
CLI surface around it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.console import Console
from rich.prompt import Prompt

from velune.cli import design
from velune.recovery import (
    SUBSYSTEMS,
    archive_has_encrypted_secrets,
    create_backup,
    restore_backup,
)

console = Console()


def _parse_include(raw: str) -> set[str] | None:
    """Parse a comma-separated subsystem filter, validating each name."""
    if not raw:
        return None
    names = {n.strip() for n in raw.split(",") if n.strip()}
    unknown = names - set(SUBSYSTEMS)
    if unknown:
        console.print(
            f"[{design.DANGER}]Unknown subsystem(s): {', '.join(sorted(unknown))}[/{design.DANGER}]"
        )
        console.print(f"[{design.MUTED}]Valid: {', '.join(SUBSYSTEMS)}[/{design.MUTED}]")
        raise typer.Exit(1)
    return names


def backup_cmd(
    output: str = typer.Option(
        "", "--output", "-o", help="Output archive path (default: velune-backup-YYYYMMDD.tar.gz)"
    ),
    include: str = typer.Option(
        "", "--include", "-i", help=f"Comma-separated subsystems ({', '.join(SUBSYSTEMS)})"
    ),
    with_secrets: bool = typer.Option(
        False,
        "--with-secrets",
        help="Include provider API keys, encrypted with a passphrase you provide",
    ),
    no_secrets: bool = typer.Option(
        False,
        "--no-secrets",
        help="[deprecated, now the default] Provider API keys are excluded/masked unless --with-secrets is set",
    ),
) -> None:
    """Snapshot all Velune state (sessions, config, providers, memory, trust).

    Provider API keys are excluded (masked as "***") by default. Pass
    --with-secrets to include them — you will be prompted for a passphrase,
    and the keys are AES-GCM encrypted with it before being written; they are
    never stored in the clear.
    """
    selected = _parse_include(include)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    dest = Path(output) if output else Path.cwd() / f"velune-backup-{date_str}.tar.gz"

    passphrase = None
    if with_secrets:
        passphrase = Prompt.ask(
            "Passphrase to encrypt provider API keys in this archive", password=True
        )
        confirm = Prompt.ask("Confirm passphrase", password=True)
        if passphrase != confirm:
            console.print(f"[{design.DANGER}]Passphrases did not match.[/{design.DANGER}]")
            raise typer.Exit(1)

    console.print(f"[{design.MUTED}]Building backup...[/{design.MUTED}]")
    result = create_backup(
        dest, include=selected, with_secrets=with_secrets, secrets_passphrase=passphrase
    )

    console.print(f"[{design.OK}]Backup written:[/{design.OK}] {result.path}")
    size_kb = result.size_bytes / 1024
    console.print(f"[{design.MUTED}]Size: {size_kb:.1f} KB[/{design.MUTED}]")

    for name in SUBSYSTEMS:
        summary = result.subsystems.get(name)
        if summary is None:
            continue
        console.print(f"  [{design.ACCENT}]{name}[/{design.ACCENT}]: {_summarize(name, summary)}")

    if result.with_secrets and "providers" in result.subsystems:
        console.print(
            f"[{design.WARN}]Provider API keys are encrypted in this archive with your "
            f"passphrase — it is not stored anywhere, so keep it safe; it's required to "
            f"restore them.[/{design.WARN}]"
        )


def restore_cmd(
    archive: str = typer.Argument(..., help="Path to a velune backup .tar.gz archive"),
    include: str = typer.Option(
        "", "--include", "-i", help=f"Comma-separated subsystems ({', '.join(SUBSYSTEMS)})"
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Overwrite existing sessions/config/trust files"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be restored without writing"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Restore Velune state from a backup archive onto this machine."""
    src = Path(archive)
    if not src.is_file():
        console.print(f"[{design.DANGER}]Archive not found: {src}[/{design.DANGER}]")
        raise typer.Exit(1)

    selected = _parse_include(include)

    if not dry_run and not yes:
        confirmed = typer.confirm(
            "Restore will write files into your Velune state. Continue?", default=True
        )
        if not confirmed:
            console.print(f"[{design.MUTED}]Aborted.[/{design.MUTED}]")
            return

    passphrase = None
    if (selected is None or "providers" in selected) and archive_has_encrypted_secrets(src):
        passphrase = Prompt.ask(
            "Passphrase to decrypt provider API keys in this archive "
            "(leave blank to skip restoring them)",
            password=True,
            default="",
            show_default=False,
        )
        passphrase = passphrase or None

    try:
        result = restore_backup(
            src,
            include=selected,
            overwrite=overwrite,
            dry_run=dry_run,
            secrets_passphrase=passphrase,
        )
    except (ValueError, FileNotFoundError) as exc:
        console.print(f"[{design.DANGER}]{exc}[/{design.DANGER}]")
        raise typer.Exit(1)

    created = result.manifest.get("created_at", "unknown")
    label = "Would restore" if dry_run else "Restored"
    console.print(f"[{design.MUTED}]Backup from:[/{design.MUTED}] {created}")

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
        hint = " (use --overwrite to replace existing files)" if not overwrite else ""
        console.print(f"[{design.MUTED}]Nothing restored{hint}.[/{design.MUTED}]")
    elif dry_run:
        console.print(
            f"[{design.MUTED}]Dry run — no files written. Re-run without --dry-run to apply.[/{design.MUTED}]"
        )


def _summarize(name: str, summary: dict) -> str:
    """One-line, human-readable description of a subsystem's backup payload."""
    if name == "sessions":
        return f"{len(summary.get('files', []))} session(s)"
    if name == "config":
        return f"{len(summary.get('files', []))} file(s)"
    if name == "providers":
        provs = summary.get("providers", [])
        return ", ".join(provs) if provs else "none"
    if name == "memory":
        parts = []
        if summary.get("db"):
            parts.append("cognitive DB")
        if summary.get("lancedb"):
            parts.append("LanceDB")
        return ", ".join(parts) if parts else "none"
    if name == "trust":
        return "present" if summary.get("files") else "none"
    return ""
