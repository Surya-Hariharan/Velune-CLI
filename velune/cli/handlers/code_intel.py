"""Code intelligence slash command handlers: /lint /refactor /types."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL

_log = logging.getLogger("velune.cli.handlers.code_intel")


def _resolve_workspace_path(repl: VeluneREPL, file_arg: str) -> Path | None:
    """Resolve *file_arg* relative to the runtime workspace."""
    workspace_raw = repl.container.get("runtime.workspace")
    workspace = Path(workspace_raw) if workspace_raw else Path.cwd()
    candidate = (workspace / file_arg.strip()).resolve()
    try:
        candidate.relative_to(workspace.resolve())
        if candidate.is_file():
            return candidate
    except ValueError:
        pass
    return None


async def cmd_lint(repl: VeluneREPL, args: str) -> None:
    from velune.analysis.linter import PythonLinter, render_lint_panel

    target = args.strip()
    if target:
        path = _resolve_workspace_path(repl, target)
        if path is None:
            repl.console.print(f"[yellow]File not found:[/yellow] {target}")
            return
        paths = [path]
    else:
        paths = []
        for msg in reversed(repl._conversation[-10:]):
            content = msg.get("content", "") if isinstance(msg, dict) else ""
            for token in content.split():
                if token.startswith("@") and token.endswith(".py"):
                    p = _resolve_workspace_path(repl, token[1:])
                    if p and p not in paths:
                        paths.append(p)
        if not paths:
            repl.console.print("[dim]Usage: /lint <file.py>  (or @mention a file first)[/dim]")
            return

    linter = PythonLinter()
    any_issues = False
    for path in paths:
        diags = await asyncio.to_thread(linter.lint_file, path)
        if diags:
            any_issues = True
            render_lint_panel(repl.console, path.name, diags)
        else:
            repl.console.print(f"[green]{path.name}[/green] [dim]No issues found.[/dim]")
    if not any_issues and len(paths) > 1:
        repl.console.print("[green]All files clean.[/green]")


async def cmd_refactor(repl: VeluneREPL, args: str) -> None:
    from rich.table import Table

    from velune.analysis.refactor import RefactorAnalyzer
    from velune.cli import design

    target = args.strip()
    if not target:
        repl.console.print("[yellow]Usage: /refactor <file.py>[/yellow]")
        return

    path = _resolve_workspace_path(repl, target)
    if path is None:
        repl.console.print(f"[yellow]File not found:[/yellow] {target}")
        return

    analyzer = RefactorAnalyzer()
    hints = await asyncio.to_thread(analyzer.analyze_file, path)

    if not hints:
        repl.console.print(f"[green]{path.name}[/green] [dim]No smells detected.[/dim]")
        return

    tbl = Table(
        title=f"Refactor hints · {path.name}",
        border_style="dim",
        padding=(0, 1),
        show_lines=False,
    )
    tbl.add_column("Rule", style=f"bold {design.ACCENT}", no_wrap=True)
    tbl.add_column("Line", style="dim", no_wrap=True)
    tbl.add_column("Function", style="cyan")
    tbl.add_column("Issue")
    tbl.add_column("Suggestion", style="dim")

    for h in hints:
        color = design.DANGER if h.severity == "error" else design.WARN
        tbl.add_row(
            f"[{color}]{h.rule_id}[/{color}]",
            str(h.line),
            h.function_name or "—",
            h.message,
            h.suggestion,
        )
    repl.console.print(tbl)


async def cmd_typify(repl: VeluneREPL, args: str) -> None:
    from rich.panel import Panel
    from rich.syntax import Syntax

    from velune.analysis.type_inferrer import TypeInferrer
    from velune.cli import design

    target = args.strip()
    if not target:
        repl.console.print("[yellow]Usage: /types <file.py>[/yellow]")
        return

    path = _resolve_workspace_path(repl, target)
    if path is None:
        repl.console.print(f"[yellow]File not found:[/yellow] {target}")
        return

    inferrer = TypeInferrer()
    suggestions = await asyncio.to_thread(inferrer.infer_file, path)

    if not suggestions:
        repl.console.print(
            f"[green]{path.name}[/green] [dim]All functions already annotated.[/dim]"
        )
        return

    diff_str = await asyncio.to_thread(
        inferrer._render_suggestions,
        path.read_text(encoding="utf-8", errors="replace"),
        suggestions,
    )

    repl.console.print(
        Panel(
            Syntax(diff_str, "diff", theme="monokai", line_numbers=False),
            title=f"[{design.ACCENT}]Type suggestions · {path.name}[/{design.ACCENT}]  "
            f"[dim]{len(suggestions)} function(s)[/dim]",
            border_style="dim",
            padding=(0, 1),
        )
    )

    answer = await asyncio.to_thread(input, "Apply suggestions? [y/N] ")
    if answer.strip().lower() == "y":
        patched = inferrer.apply_suggestions(
            path.read_text(encoding="utf-8", errors="replace"),
            suggestions,
        )
        path.write_text(patched, encoding="utf-8")
        repl.console.print(f"[green]Annotations written to {path.name}[/green]")
    else:
        repl.console.print("[dim]No changes made.[/dim]")
