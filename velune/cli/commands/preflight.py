"""Preflight check validation for model availability and workspace initialization."""

from __future__ import annotations

from pathlib import Path
from rich.box import ROUNDED
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from velune.kernel.registry import ServiceContainer


async def run_preflight_check(container: ServiceContainer, console: Console | None = None) -> bool:
    """Runs preflight checks for models and workspace state.

    If checks fail, displays a gorgeous error panel with copy-pasteable fix commands and returns False.
    Otherwise returns True.
    """
    issues = []

    # 1. Check workspace initialization
    workspace = container.get("runtime.workspace")
    if not isinstance(workspace, Path):
        workspace = Path(workspace)

    # We check for the presence of the Tree-sitter AST index folder or `.velune` directory structure
    if not (workspace / ".velune" / "index").exists():
        issues.append(
            ("Workspace has not been initialized yet.\n"
             "  [bold white]Fix:[/bold white] Run the initialization command to parse the codebase:\n"
             "       [bold green]velune workspace init[/bold green]")
        )

    # 2. Check models registry
    registry = container.get("runtime.model_registry")
    models = registry.list_all()
    if not models:
        issues.append(
            ("No model providers or local LLM instances were detected.\n"
             "  [bold white]Fix:[/bold white] Make sure Ollama/LM-Studio is running, or check API keys, and run:\n"
             "       [bold green]velune models scan --probe[/bold green]")
        )

    if issues:
        if console:
            console.print()

            body_elements = [
                "[bold red]Velune preflight check failed with the following blocking issues:[/bold red]\n"
            ]
            for i, issue in enumerate(issues, 1):
                body_elements.append(f"\n[bold red]{i}.[/bold red] {issue}\n")

            body_elements.append(
                "\n[dim]Ensure these preflight requirements are satisfied before running Reasoning Council tasks.[/dim]"
            )

            panel_content = Text.from_markup("".join(body_elements))

            console.print(
                Panel(
                    panel_content,
                    title="[bold red]⚠️ Preflight Check Blocked[/bold red]",
                    border_style="red",
                    box=ROUNDED,
                    padding=(1, 2),
                )
            )
            console.print()
        return False

    return True
