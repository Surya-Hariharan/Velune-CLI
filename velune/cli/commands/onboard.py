"""velune onboard — guided first-time onboarding wizard.

Runs the structured 8-stage onboarding lifecycle. Can be invoked explicitly
to re-run or resume a partial setup.

Stages
──────
  1  Welcome               — brand panel + mode selection
  2  Detect Environment    — hardware scan (CPU/GPU/VRAM/tier)
  3  Configure Providers   — local + cloud provider setup
  4  Discover Models       — scan all configured providers
  5  Select Default Model  — pick and persist the default model
  6  Health Check          — 6 critical system checks
  7  Workspace Setup       — .velune/ tree + workspace registration
  8  Ready                 — summary panel + launch chat

Usage
─────
  velune onboard              # resume from last incomplete stage
  velune onboard --restart    # start over from Stage 1
  velune onboard --stage 6    # jump directly to Stage 6 (Health Check)
"""

from __future__ import annotations

import typer

from velune.cli.context import CLIContext
from velune.cli.interactive.tty import is_interactive_tty
from velune.cli.onboarding import _STAGE_NAMES, load_stage_progress, run_onboarding


def onboard_command(
    ctx: typer.Context,
    resume: bool = typer.Option(
        True,
        "--resume/--restart",
        help="Resume from last completed stage (default) or restart from Stage 1.",
    ),
    stage: int = typer.Option(
        0,
        "--stage",
        "-s",
        min=0,
        max=8,
        help="Jump directly to a specific stage number (1–8). Overrides --resume.",
    ),
) -> None:
    """Run the first-time setup wizard, or resume an incomplete run."""
    cli_ctx: CLIContext = ctx.obj
    if not isinstance(cli_ctx, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    if not is_interactive_tty():
        typer.echo(
            "velune onboard needs an interactive terminal. Set provider API keys via"
            " environment variables instead (see `velune doctor` for what's missing)."
        )
        raise typer.Exit(1)

    runtime = cli_ctx.runtime

    # Compute the 0-indexed start stage.
    start_stage = 0
    if stage:
        start_stage = max(0, stage - 1)
    elif resume:
        completed = load_stage_progress()
        for i, name in enumerate(_STAGE_NAMES):
            if name not in completed:
                start_stage = i
                break
        # else: all stages completed — re-run from beginning (start_stage stays 0)

    run_onboarding(runtime, start_stage=start_stage)
