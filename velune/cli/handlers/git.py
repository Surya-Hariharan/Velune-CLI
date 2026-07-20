"""Git-related slash command handlers: /diff /undo /hunk /push /pr /issue."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL

_log = logging.getLogger("velune.cli.handlers.git")


async def cmd_diff(repl: VeluneREPL, args: str) -> None:
    import subprocess

    from rich.panel import Panel
    from rich.syntax import Syntax

    workspace = repl.container.get("runtime.workspace")
    stat = await asyncio.to_thread(
        subprocess.run,
        ["git", "diff", "--stat"],
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if not stat.stdout.strip():
        repl.console.print("[dim]No uncommitted changes.[/dim]")
        return

    full = await asyncio.to_thread(
        subprocess.run,
        ["git", "diff"],
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    stat_text = stat.stdout.strip()
    diff_content = full.stdout[:8000] if full.stdout else ""
    if len(full.stdout or "") > 8000:
        diff_content += "\n... [diff truncated]"

    body = (
        Syntax(diff_content, "diff", theme="monokai", line_numbers=False)
        if diff_content
        else stat_text
    )
    repl.console.print(
        Panel(
            body,
            title=f"[yellow]Working Tree Diff[/yellow]  [dim]{stat_text.splitlines()[-1]}[/dim]",
            border_style="yellow",
            padding=(0, 1),
        )
    )


async def cmd_undo(repl: VeluneREPL, args: str) -> None:
    """Revert the last Velune-generated git commit, keeping changes staged."""
    import subprocess
    from pathlib import Path as _Path

    workspace = _Path(repl.container.get("runtime.workspace") or ".").resolve()

    log = await asyncio.to_thread(
        subprocess.run,
        ["git", "log", "-1", "--format=%s%n%b"],
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if log.returncode != 0:
        repl.console.print("[red]No git repository found or git log failed.[/red]")
        return

    last_msg = log.stdout.strip().lower()
    is_velune_commit = "velune:" in last_msg or "co-authored-by: velune" in last_msg

    if not is_velune_commit:
        repl.console.print(
            "[yellow]Last commit is not a Velune-generated commit — undo aborted.[/yellow]\n"
            "[dim]Only commits created by Velune's edit pipeline can be undone with /undo.[/dim]"
        )
        return

    reset = await asyncio.to_thread(
        subprocess.run,
        ["git", "reset", "--soft", "HEAD^"],
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if reset.returncode == 0:
        repl.console.print(
            "[green]Undo successful.[/green] "
            "[dim]Last Velune commit reverted — changes kept staged.[/dim]"
        )
    else:
        repl.console.print(f"[red]Undo failed:[/red] {reset.stderr.strip()}")


async def cmd_hunk(repl: VeluneREPL, args: str) -> None:
    """Toggle hunk-by-hunk review mode for edit sessions."""
    repl._hunk_review_mode = not repl._hunk_review_mode
    state = "enabled" if repl._hunk_review_mode else "disabled"
    repl.console.print(
        f"[cyan]Hunk review mode {state}.[/cyan] "
        f"[dim]{'Each hunk in a diff will be reviewed individually.' if repl._hunk_review_mode else 'Diffs are reviewed file-by-file (default).'}[/dim]"
    )


async def cmd_push(repl: VeluneREPL, args: str) -> None:
    """Push the current branch to origin."""
    from velune.tools.git.providers import GitPushTool

    force = "--force" in args or "-f" in args
    workspace_raw = repl.container.get("runtime.workspace")
    workspace = Path(workspace_raw) if workspace_raw else Path.cwd()

    try:
        tool = GitPushTool(workspace=workspace)
        with repl.console.status("[cyan]Pushing branch to remote…[/cyan]"):
            result = await tool.execute(force=force)
        repl.console.print(f"[green]{result}[/green]")
    except Exception as exc:
        repl.console.print(f"[red]Push failed:[/red] {exc}")


async def cmd_pr(repl: VeluneREPL, args: str) -> None:
    """Create a pull request / merge request on GitHub or GitLab.

    Usage: /pr <title> [--base <branch>] [--draft]
    """
    import shlex

    from velune.tools.git.providers import CreatePRTool, GitPushTool

    workspace_raw = repl.container.get("runtime.workspace")
    workspace = Path(workspace_raw) if workspace_raw else Path.cwd()

    tokens = shlex.split(args) if args.strip() else []
    draft = "--draft" in tokens
    base = "main"
    title_parts: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "--draft":
            i += 1
            continue
        if t == "--base" and i + 1 < len(tokens):
            base = tokens[i + 1]
            i += 2
            continue
        title_parts.append(t)
        i += 1
    title = " ".join(title_parts).strip()

    if not title:
        repl.console.print(
            "[dim]Usage:[/dim] [bold]/pr[/bold] <title> [--base <branch>] [--draft]\n"
            "[dim]Example:[/dim] /pr 'Add retry logic' --base main"
        )
        return

    try:
        push_tool = GitPushTool(workspace=workspace)
        with repl.console.status("[cyan]Pushing branch…[/cyan]"):
            push_result = await push_tool.execute(set_upstream=True)
        repl.console.print(f"[dim]{push_result}[/dim]")
    except Exception as exc:
        repl.console.print(
            f"[yellow]Warning: push step failed ({exc}) — continuing with PR creation.[/yellow]"
        )

    try:
        pr_tool = CreatePRTool(workspace=workspace)
        with repl.console.status("[cyan]Creating pull request…[/cyan]"):
            pr = await pr_tool.execute(title=title, base=base, draft=draft)

        badge = "[dim][DRAFT][/dim] " if pr.get("draft") else ""
        repl.console.print(
            f"\n[green]PR #{pr['pr_number']} created[/green] {badge}on {pr.get('provider', 'remote')}\n"
            f"  [bold]{pr['title']}[/bold]\n"
            f"  [link={pr['url']}]{pr['url']}[/link]"
        )
    except Exception as exc:
        repl.console.print(f"[red]PR creation failed:[/red] {exc}")


async def cmd_issue(repl: VeluneREPL, args: str) -> None:
    """Fetch a GitHub/GitLab issue and inject its body as context.

    Usage: /issue <number>
    """
    from velune.tools.git.providers import GetIssueTool

    workspace_raw = repl.container.get("runtime.workspace")
    workspace = Path(workspace_raw) if workspace_raw else Path.cwd()

    issue_num_str = args.strip().lstrip("#")
    if not issue_num_str.isdigit():
        repl.console.print(
            "[dim]Usage:[/dim] [bold]/issue[/bold] <number>   [dim]e.g. /issue 42[/dim]"
        )
        return

    issue_number = int(issue_num_str)
    try:
        tool = GetIssueTool(workspace=workspace)
        with repl.console.status(f"[cyan]Fetching issue #{issue_number}…[/cyan]"):
            issue = await tool.execute(issue_number=issue_number)

        state_color = "green" if issue["state"] == "open" else "red"
        labels = "  ".join(f"[dim][{lbl}][/dim]" for lbl in issue.get("labels", []))
        repl.console.print(
            f"\n[{state_color}][bold]#{issue['number']} {issue['title']}[/bold][/{state_color}]  {labels}\n"
            f"[link={issue['url']}]{issue['url']}[/link]\n"
        )
        if issue.get("body"):
            from rich.markdown import Markdown

            repl.console.print(Markdown(issue["body"][:2000]))

        context_block = (
            f"[Issue #{issue['number']} from {issue['provider']}]\n"
            f"Title: {issue['title']}\n"
            f"State: {issue['state']}\n"
            f"Labels: {', '.join(issue.get('labels', []))}\n\n"
            f"{issue.get('body', '')}"
        )
        repl._conversation.append(
            {
                "role": "assistant",
                "content": f"I've loaded issue #{issue['number']} into context:\n\n{context_block}",
            }
        )
        repl.console.print(
            f"\n[dim]Issue #{issue['number']} injected into conversation context. "
            "Your next message can reference it directly.[/dim]"
        )
    except Exception as exc:
        repl.console.print(f"[red]Failed to fetch issue #{issue_number}:[/red] {exc}")
