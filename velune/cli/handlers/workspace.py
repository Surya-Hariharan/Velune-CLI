"""Workspace/project slash command handlers: /project and workspace switching."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL

_log = logging.getLogger("velune.cli.handlers.workspace")


async def cmd_project(repl: VeluneREPL, args: str) -> None:
    """Manage project workspaces: open, close, status, add, list, switch."""
    parts = args.strip().split(None, 1)
    sub = parts[0].lower() if parts else ""
    sub_args = parts[1] if len(parts) > 1 else ""

    if sub == "open":
        await _project_open(repl, sub_args.strip())
        return

    if sub == "close":
        await _project_close(repl)
        return

    if sub == "status":
        await _project_status(repl)
        return

    if sub == "add":
        target = Path(sub_args.strip() or ".").expanduser()
        if not target.is_dir():
            repl.console.print(f"[red]Not a directory: {target}[/red]")
            return
        info = repl._workspace_registry.register(target)
        kind = info.project_type or ("git repo" if info.is_git else "folder")
        repl.console.print(
            f"[green]Registered workspace:[/green] [cyan]{info.name}[/cyan] [dim]({kind})[/dim]"
        )
        return

    if sub == "list":
        await _project_list(repl)
        return

    if sub and sub not in ("switch",):
        await _project_switch_target(repl, args.strip())
        return

    if sub == "switch" and sub_args:
        await _project_switch_target(repl, sub_args.strip())
        return

    await _project_picker(repl)


async def _project_open(repl: VeluneREPL, raw_path: str) -> None:
    """Register and activate *raw_path* as the workspace (no cognition)."""
    target = Path(raw_path or ".").expanduser()
    if not target.is_dir():
        repl.console.print(f"[red]Path does not exist or is not a directory:[/red] {target}")
        return
    target = target.resolve()
    repl._workspace_registry.register(target)
    current = Path(repl.container.get("runtime.workspace")).resolve()
    if target == current:
        repl.console.print(f"[dim]Already in this workspace:[/dim] [cyan]{target.name}[/cyan]")
        return
    await switch_workspace(repl, target)
    repl.console.print(
        "[dim]→ Workspace registered. Run [bold]/index[/bold] to analyze it.[/dim]"
    )


async def _project_close(repl: VeluneREPL) -> None:
    """Leave the current project, reverting the workspace to the launch directory."""
    home = Path.home().resolve()
    current = Path(repl.container.get("runtime.workspace")).resolve()
    if current == home:
        repl.console.print("[dim]No project is open.[/dim]")
        return
    await switch_workspace(repl, home)
    repl.console.print("[dim]Project closed.[/dim]")


async def _project_status(repl: VeluneREPL) -> None:
    from rich.panel import Panel

    workspace = Path(repl.container.get("runtime.workspace")).resolve()
    info = repl._workspace_registry.get(workspace)
    is_git = (workspace / ".git").exists()
    indexed = (workspace / ".velune" / "index_state.json").exists()
    ptype = (info.project_type if info else None) or ("git repo" if is_git else "folder")
    model = repl.active_model.model_id if repl.active_model else "[dim]none[/dim]"
    repl.console.print(
        Panel(
            f"[bold]Workspace[/bold]  [cyan]{workspace.name}[/cyan]\n"
            f"[bold]Path[/bold]       {workspace}\n"
            f"[bold]Type[/bold]       {ptype}\n"
            f"[bold]Git[/bold]        {'yes' if is_git else 'no'}\n"
            f"[bold]Indexed[/bold]    {'yes' if indexed else 'no — run /index'}\n"
            f"[bold]Model[/bold]      {model}",
            title="Project Status",
            border_style="dim",
        )
    )


async def _project_list(repl: VeluneREPL) -> None:
    from rich.table import Table

    workspaces = repl._workspace_registry.list()
    if not workspaces:
        repl.console.print("[dim]No workspaces registered. Use /project add <path>.[/dim]")
        return
    current = str(Path(repl.container.get("runtime.workspace")).resolve())
    table = Table(border_style="dim", padding=(0, 1))
    table.add_column("Project", style="cyan")
    table.add_column("Type", style="dim")
    table.add_column("Last Opened", style="dim")
    table.add_column("Path", style="dim")
    for w in workspaces:
        name = f"{w.name} [green](current)[/green]" if w.path == current else w.name
        table.add_row(
            name,
            w.project_type or ("git" if w.is_git else "—"),
            w.last_opened[:16].replace("T", " "),
            w.path,
        )
    repl.console.print(table)


async def _project_picker(repl: VeluneREPL) -> None:
    from velune.cli.picker import PickItem, pick

    workspaces = repl._workspace_registry.list()
    if not workspaces:
        repl.console.print(
            "[dim]No workspaces registered yet. Use /project add <path> to register one.[/dim]"
        )
        return
    current = str(Path(repl.container.get("runtime.workspace")).resolve())
    items = [
        PickItem(
            id=w.path,
            label=w.name,
            meta=w.project_type or ("git" if w.is_git else ""),
            group="Projects",
            is_current=(w.path == current),
        )
        for w in workspaces
    ]
    chosen = await pick("Project workspaces", items)
    if chosen is None or chosen.is_current:
        return
    await switch_workspace(repl, Path(chosen.id))


async def _project_switch_target(repl: VeluneREPL, target: str) -> None:
    """Resolve *target* as a registered name or a filesystem path, then switch."""
    info = repl._workspace_registry.find_by_name(target)
    if info is not None:
        await switch_workspace(repl, Path(info.path))
        return
    path = Path(target).expanduser()
    if path.is_dir():
        await switch_workspace(repl, path)
        return
    repl.console.print(
        f"[red]Unknown project: {target!r}[/red]  "
        "[dim]Use /project list or /project add <path>.[/dim]"
    )


async def switch_workspace(repl: VeluneREPL, new_path: Path) -> None:
    """Swap the active project workspace inside the running session."""
    from velune.cli.workspaces import switch_workspace as _sw

    new_path = new_path.resolve()
    old_path = Path(repl.container.get("runtime.workspace")).resolve()
    if new_path == old_path:
        repl.console.print("[dim]Already in this workspace.[/dim]")
        return

    repl.console.print(f"[dim]Switching workspace → {new_path.name}...[/dim]")

    try:
        repl._archive_current_session()
    except Exception as exc:
        _log.warning("Could not archive session before switch: %s", exc)
    await repl._end_episodic_session()
    try:
        repl.container.get("runtime.working_memory").clear()
    except Exception:
        pass

    notes = await _sw(repl.container, new_path)

    repl._conversation = []
    repl.session_tokens = 0
    repl.session_cost = 0.0
    repl._project_profile = repl._load_project_profile()
    repl._workspace_registry.touch(new_path)
    await repl._start_episodic_session()

    kind = None
    if repl._project_profile:
        if isinstance(repl._project_profile, dict):
            kind = repl._project_profile.get("display_name")
        else:
            kind = getattr(repl._project_profile, "display_name", None)
    detail = f" [dim]({kind})[/dim]" if kind else ""
    repl.console.print(
        f"[green]Workspace:[/green] [cyan]{new_path.name}[/cyan]{detail}  "
        f"[dim]{notes[0] if notes else ''}[/dim]"
    )
    for note in notes[1:]:
        repl.console.print(f"  [yellow]{note}[/yellow]")
