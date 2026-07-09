"""velune setup — configure providers and models interactively.

Thin redirect into the same 8-stage onboarding wizard used by `velune onboard`,
jumped straight to the Providers stage (index 2). Kept as a separate command
name for discoverability ("setup" reads as "(re)configure providers"), but
there is only one wizard implementation now — see `velune/cli/onboarding/`.
"""

from __future__ import annotations

import typer

from velune.cli.context import CLIContext
from velune.cli.interactive.tty import is_interactive_tty
from velune.cli.onboarding import run_onboarding


def setup_command(ctx: typer.Context) -> None:
    """Configure AI provider API keys."""
    cli_ctx: CLIContext = ctx.obj
    if not isinstance(cli_ctx, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    if not is_interactive_tty():
        typer.echo(
            "velune setup needs an interactive terminal. Set provider API keys via"
            " environment variables instead (see `velune doctor` for what's missing)."
        )
        raise typer.Exit(1)

    run_onboarding(cli_ctx.runtime, start_stage=2)
