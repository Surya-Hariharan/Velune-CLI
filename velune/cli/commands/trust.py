"""``velune trust`` — manage the per-directory workspace trust list.

A trusted directory is allowed to load project-level ``.mcp.json`` /
``velune.toml`` config, which can spawn local MCP server processes and override
provider ``base_url``s. Untrusted directories fall back to user-level config
only. See :mod:`velune.core.trust`.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from velune.cli import design
from velune.core import trust

trust_cmd = typer.Typer(
    name="trust",
    help="Trust, list, or revoke workspace directories.",
    no_args_is_help=False,
)

_console = Console()


@trust_cmd.callback(invoke_without_command=True)
def _default(ctx: typer.Context) -> None:
    """Show whether the current directory is trusted (when no subcommand given)."""
    if ctx.invoked_subcommand is not None:
        return
    cwd = Path.cwd()
    if trust.is_trusted(cwd):
        _console.print(f"[{design.OK}]✓ trusted[/{design.OK}] [dim]{cwd}[/dim]")
    else:
        _console.print(
            f"[{design.WARN}]✗ not trusted[/{design.WARN}] [dim]{cwd}[/dim]\n"
            "[dim]Run [bold]velune trust add[/bold] to allow project-level MCP / config.[/dim]"
        )


@trust_cmd.command("add")
def add(
    path: Path = typer.Argument(None, help="Directory to trust (default: current)."),
) -> None:
    """Trust a directory so its project-level MCP / config is honored."""
    target = (path or Path.cwd()).expanduser()
    trust.trust(target)
    _console.print(f"[{design.OK}]✓ trusted[/{design.OK}] [dim]{target.resolve()}[/dim]")


@trust_cmd.command("forget")
def forget(
    path: Path = typer.Argument(None, help="Directory to revoke (default: current)."),
) -> None:
    """Revoke trust for a directory."""
    target = (path or Path.cwd()).expanduser()
    if trust.forget(target):
        _console.print(f"[{design.OK}]trust revoked[/{design.OK}] [dim]{target.resolve()}[/dim]")
    else:
        _console.print(f"[dim]{target.resolve()} was not trusted.[/dim]")


@trust_cmd.command("list")
def list_() -> None:
    """List all trusted directories."""
    entries = trust.list_trusted()
    if not entries:
        _console.print("[dim]No trusted directories.[/dim]")
        return
    for entry in entries:
        _console.print(f"[{design.OK}]✓[/{design.OK}] [dim]{entry}[/dim]")
