"""Public surface of the onboarding package.

``stages.py`` holds the async wizard driver; ``logic.py`` holds non-UI state
(progress persistence, model scoring, health checks). This module is the
stable import path both CLI entry points (``commands/onboard.py``,
``commands/setup.py``, ``app.py``) use, so the wizard internals can keep
moving without breaking those call sites.
"""

from __future__ import annotations

import asyncio

from velune.cli.onboarding.logic import (
    _STAGE_NAMES,
    load_stage_progress,
    onboarding_state,
)

__all__ = [
    "_STAGE_NAMES",
    "load_stage_progress",
    "onboarding_state",
    "run_onboarding",
]


def run_onboarding(runtime: object, start_stage: int = 0) -> None:
    """Synchronous entry point: run the full-screen wizard to completion.

    Every caller is a plain ``typer`` command body (no event loop already
    running), so driving the async wizard with ``asyncio.run`` is safe.
    """
    from velune.cli.onboarding import stages

    asyncio.run(stages.run(runtime, start_stage=start_stage))
