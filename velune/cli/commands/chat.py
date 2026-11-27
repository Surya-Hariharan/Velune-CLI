"""``velune chat``: open the interactive REPL, optionally resuming a saved session."""

from __future__ import annotations

import typer

from velune.cli.context import CLIContext


def chat_command(
    ctx: typer.Context,
    session_id: str | None = typer.Option(
        None,
        "--session",
        "-s",
        help="Resume a previous session by ID (see: velune session list)",
    ),
    plain: bool = typer.Option(
        False, "--plain", help="Plain scrolling output instead of the fullscreen UI"
    ),
) -> None:
    """Start an interactive chat session with your codebase.

    The same REPL that plain `velune` opens, with its tools and slash commands.
    """
    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    from velune.kernel.entrypoint import launch

    launch(cli_context.runtime, plain=plain, resume_session=session_id)
