"""Public surface of the onboarding package.

``stages.py`` holds the async wizard driver; ``logic.py`` holds non-UI state
(progress persistence, model scoring, health checks). This module is the
stable import path both CLI entry points (``commands/onboard.py``,
``commands/setup.py``, ``app.py``) use, so the wizard internals can keep
moving without breaking those call sites.
"""

from __future__ import annotations

from velune.cli.onboarding.logic import (
    _STAGE_NAMES,
    has_shown_alt_screen_notice,
    load_stage_progress,
    mark_alt_screen_notice_shown,
    onboarding_state,
)

__all__ = [
    "_STAGE_NAMES",
    "has_shown_alt_screen_notice",
    "load_stage_progress",
    "mark_alt_screen_notice_shown",
    "onboarding_state",
    "run_onboarding",
]


def run_onboarding(runtime: object, start_stage: int = 0) -> None:
    """Synchronous entry point: run the full-screen wizard to completion.

    Every caller is a plain ``typer`` command body (no event loop already
    running). Routes through ``run_async()`` — the single sanctioned
    ``asyncio.run`` call site — instead of calling it directly.
    """
    from velune.cli.onboarding import stages
    from velune.kernel.entrypoint import run_async

    run_async(stages.run(runtime, start_stage=start_stage))
