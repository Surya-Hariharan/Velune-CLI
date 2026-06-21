"""Council execution budget — hard time and cycle guards for agent deliberation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.cli.modes import SessionMode


@dataclass
class CouncilExecutionBudget:
    """Hard budget applied to every council deliberation run.

    All ``*_seconds`` values are wall-clock limits.  When passed to
    :meth:`CouncilOrchestrator.execute_task`, these values override the
    instance-level ``max_wall_time_seconds`` and inject per-agent
    ``asyncio.wait_for`` guards that prevent hangs from unresponsive models.

    Review cycle cap
    ----------------
    ``max_review_cycles`` is the binding cap on the debate loop.  Even when
    :func:`calculate_max_debate_turns` returns a higher number (e.g. 4 for a
    security failure), the debate stops after ``max_review_cycles`` turns so a
    single stubborn critic cannot loop a session indefinitely.
    """

    max_wall_time_seconds: int = 120
    max_tokens_per_agent: int = 4096
    max_review_cycles: int = 2
    planner_timeout_seconds: int = 30
    coder_timeout_seconds: int = 60
    reviewer_timeout_seconds: int = 30

    @classmethod
    def from_session_mode(cls, mode: SessionMode) -> CouncilExecutionBudget:
        """Return a budget calibrated to the given session mode.

        * ``OPTIMUS`` — tightest budgets; speed over depth, 1 review cycle.
        * ``NORMAL``  — balanced defaults (2 review cycles, 120s wall).
        * ``GODLY``   — relaxed but still bounded; depth over speed, 3 cycles.
        """
        from velune.cli.modes import SessionMode

        if mode == SessionMode.OPTIMUS:
            return cls(
                max_wall_time_seconds=60,
                max_tokens_per_agent=2048,
                max_review_cycles=1,
                planner_timeout_seconds=15,
                coder_timeout_seconds=30,
                reviewer_timeout_seconds=15,
            )
        if mode == SessionMode.GODLY:
            return cls(
                max_wall_time_seconds=300,
                max_tokens_per_agent=8192,
                max_review_cycles=3,
                planner_timeout_seconds=60,
                coder_timeout_seconds=120,
                reviewer_timeout_seconds=60,
            )
        # SessionMode.NORMAL — default values
        return cls()
