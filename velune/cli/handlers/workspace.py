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
    repl.console.print("[dim]→ Workspace registered. Run [bold]/index[/bold] to analyze it.[/dim]")


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
    from velune.providers.keystore import list_configured_providers
    from velune.repository.index_state import IndexState

    workspace = Path(repl.container.get("runtime.workspace")).resolve()
    is_git = (workspace / ".git").exists()

    ws_name = workspace.name
    ws_path = str(workspace)

    # Git Branch & Status
    active_branch = repl._cached_branch
    if not active_branch or active_branch == "unknown":
        if is_git:
            from velune.repository.tracker import GitTracker

            try:
                active_branch = GitTracker(workspace).get_active_branch()
            except Exception:
                active_branch = "unknown"
        else:
            active_branch = "non-git"

    repo_status = "non-git"
    modified_count = 0
    untracked_count = 0
    if is_git:
        try:
            import subprocess

            res = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    if line.startswith("??"):
                        untracked_count += 1
                    else:
                        modified_count += 1
                if modified_count == 0 and untracked_count == 0:
                    repo_status = "clean"
                else:
                    parts = []
                    if modified_count > 0:
                        parts.append(f"{modified_count} modified")
                    if untracked_count > 0:
                        parts.append(f"{untracked_count} untracked")
                    repo_status = f"dirty ({', '.join(parts)})"
            else:
                repo_status = "unknown"
        except Exception:
            repo_status = "unknown"

    # Indexed Files
    state_path = workspace / ".velune" / "index_state.json"
    index_state = IndexState.load(state_path) if state_path.exists() else None
    indexed_files = len(index_state.file_index) if index_state else 0
    indexed_str = f"{indexed_files} files indexed" if index_state else "not indexed — run /index"

    # Project Profile (Language / Framework)
    profile = repl._project_profile or repl._load_project_profile()
    language = getattr(profile, "primary_language", "unknown") if profile else "unknown"
    frameworks = getattr(profile, "detected_frameworks", []) if profile else []
    framework = ", ".join(frameworks) if frameworks else "generic / none"

    # Memory Status
    working_turns = 0
    episodic_turns = 0
    graph_nodes = 0
    try:
        working = repl.container.get("runtime.working_memory")
        working_turns = len(working.get_turns())
    except Exception:
        pass
    try:
        episodic_tier = repl.container.get("runtime.episodic_memory")
        if episodic_tier:
            episodic_turns = len(await episodic_tier.get_turns("default"))
    except Exception:
        pass
    try:
        graph = repl.container.get("runtime.graph_memory")
        if graph:
            nodes = await graph.get_all_nodes()
            graph_nodes = len(nodes)
    except Exception:
        pass

    memory_status = (
        f"Working: {working_turns} turns · "
        f"Episodic: {episodic_turns} turns · "
        f"Graph: {graph_nodes} entities"
    )

    # MCP Status
    connected_mcp = 0
    total_mcp = 0
    try:
        mcp_status = repl._mcp_registry.status()
        total_mcp = len(mcp_status)
        connected_mcp = sum(1 for s in mcp_status if s["state"] == "connected")
    except Exception:
        pass
    mcp_status_str = f"{connected_mcp} connected / {total_mcp} configured"

    # Connected Providers
    try:
        configured_providers = list_configured_providers()
    except Exception:
        configured_providers = []
    providers_str = ", ".join(configured_providers) if configured_providers else "none connected"

    # Current Model
    model = repl.active_model.model_id if repl.active_model else "none selected"

    # Recent Tasks
    recent_jobs = []
    if repl._job_registry:
        try:
            jobs = repl._job_registry.all_jobs()
            jobs.sort(key=lambda j: j.submitted_at, reverse=True)
            recent_jobs = jobs[:5]
        except Exception:
            pass

    # Pending Warnings
    pending_warnings = []
    if repl._alert_store:
        try:
            pending_warnings = [a.title for a in repl._alert_store.all_alerts()]
        except Exception:
            pass

    if not is_git:
        pending_warnings.append("Not a git repository")
    if not index_state:
        pending_warnings.append("Workspace is not indexed — run /index to analyze files")
    if not repl.active_model:
        pending_warnings.append("No active model selected — run /model to choose one")
    if not configured_providers:
        pending_warnings.append("No AI providers configured — run /providers to add API keys")

    # Clean table layout
    from velune.cli.ui_components import create_table, print_header, print_notification

    table = create_table("Key", "Value")

    table.add_row("Workspace", ws_name)
    table.add_row("Path", ws_path)
    table.add_row("Git Branch", active_branch)
    table.add_row("Repository Status", repo_status)
    table.add_row("Indexed Files", indexed_str)
    table.add_row("Language", language)
    table.add_row("Framework", framework)
    table.add_row("", "")
    table.add_row("Current Model", model)
    table.add_row("Connected Providers", providers_str)
    table.add_row("MCP Status", mcp_status_str)
    table.add_row("Memory Status", memory_status)

    print_header(repl.console, "Workspace Overview")
    repl.console.print(table)
    repl.console.print()

    if recent_jobs:
        print_header(repl.console, "Recent Tasks")
        for job in recent_jobs:
            status_color = (
                "yellow"
                if job.status.value == "running"
                else ("green" if job.status.value == "completed" else "red")
            )
            repl.console.print(
                f"  [cyan]{job.job_id:<10}[/cyan]  {job.description[:45]:<45}  [{status_color}]{job.status.value}[/{status_color}]"
            )
        repl.console.print()

    print_header(repl.console, "Pending Warnings")
    if pending_warnings:
        for warn in pending_warnings:
            print_notification(repl.console, warn, type="warning")
    else:
        print_notification(repl.console, "No pending warnings. System is healthy.", type="success")
    repl.console.print()


async def _project_list(repl: VeluneREPL) -> None:
    from velune.cli.ui_components import create_table, print_notification

    workspaces = repl._workspace_registry.list()
    if not workspaces:
        print_notification(
            repl.console, "No workspaces registered. Use /project add <path>.", type="info"
        )
        return
    current = str(Path(repl.container.get("runtime.workspace")).resolve())
    table = create_table("Project", "Type", "Last Opened", "Path")
    for w in workspaces:
        name = f"[bold]{w.name}[/bold] [green](current)[/green]" if w.path == current else w.name
        table.add_row(
            name,
            w.project_type or ("git" if w.is_git else "—"),
            w.last_opened[:16].replace("T", " "),
            w.path,
        )
    repl.console.print(table)
    repl.console.print()


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
