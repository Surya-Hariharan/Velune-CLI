"""Workspace command - velune workspace init/status."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

console = Console()

workspace_cmd = typer.Typer(help="Workspace management commands")


@workspace_cmd.command("init")
def workspace_init(
    path: Path = typer.Argument(Path.cwd(), help="Workspace path"),
    force: bool = typer.Option(False, "--force", "-f", help="Force reinitialization"),
) -> None:
    """Initialize a Velune workspace."""
    console.print(f"[yellow]Initializing workspace at {path}[/yellow]")
    
    # Create .velune directory
    velune_dir = path / ".velune"
    velune_dir.mkdir(exist_ok=True)
    
    # Create subdirectories
    (velune_dir / "memory").mkdir(exist_ok=True)
    (velune_dir / "retrieval").mkdir(exist_ok=True)
    (velune_dir / "index").mkdir(exist_ok=True)
    
    console.print(f"[green]✓[/green] Created .velune directory structure")
    console.print("[green]✓[/green] Workspace initialized")
    
    console.print(Panel.fit(
        f"Workspace: {path}\n"
        f"Velune dir: {velune_dir}",
        title="Workspace Status",
    ))


@workspace_cmd.command("status")
def workspace_status(
    path: Path = typer.Option(Path.cwd(), "--path", "-p", help="Workspace path"),
) -> None:
    """Show workspace status."""
    velune_dir = path / ".velune"
    
    if not velune_dir.exists():
        console.print("[red]✗[/red] Not a Velune workspace (no .velune directory)")
        return
    
    console.print(Panel.fit(
        f"Workspace: {path}\n"
        f"Velune dir: {velune_dir}\n"
        f"Status: [green]Initialized[/green]",
        title="Workspace Status",
    ))
