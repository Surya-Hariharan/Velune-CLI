"""Interactive ask command boundary."""

from __future__ import annotations

import asyncio
from typing import Optional

import typer
from rich.panel import Panel

from velune.cli.context import CLIContext
from velune.core.async_runtime import run_async

ask_cmd = typer.Typer(help="Interactive prompt entry point")


@ask_cmd.callback(invoke_without_command=True)
def ask_command(
    ctx: typer.Context,
    prompt: Optional[str] = typer.Argument(None, help="Prompt to route through Velune"),
) -> None:
    """Open the orchestration boundary for a natural-language task."""

    if ctx.invoked_subcommand is not None:
        return

    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI runtime context was not initialized")

    message = prompt or "Enter a task description to route through Velune."
    result = run_async(_prepare_prompt(cli_context, message))
    cli_context.console.print(
        Panel(
            "\n".join(
                [
                    f"[bold]Workspace:[/bold] {cli_context.workspace}",
                    f"[bold]Config:[/bold] {cli_context.config_path or 'auto-discovered'}",
                    f"[bold]Prompt:[/bold] {result}",
                    "",
                    "[dim]The orchestration engine, memory, and model router will attach here.[/dim]",
                ]
            ),
            title="Velune Ask",
        )
    )


async def _prepare_prompt(context: CLIContext, prompt: str) -> str:
    """Async boundary for future prompt routing and orchestration."""

    await asyncio.sleep(0)
    return prompt