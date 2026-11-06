"""The supported, flag-guarded way to build a plan-and-review driver.

``create_plan_review_driver`` checks the ``orchestration.engine`` flag and raises
:class:`EngineDisabledError` while it is off, before any driver, runner or control is built.

What this does and does not guarantee
-------------------------------------
The guarantee applies to this entry point. Python cannot make direct construction impossible:
``PlanReviewDriver(...)`` remains importable and constructible by any code that wants it, and its
tests do exactly that. The only production caller is the opt-in ``velune native --plan-review`` path
(``native_session.run_native_plan_review``); no REPL, chat or MCP path calls either one, and the flag is
off by default.

This module deliberately imports neither the acting runner nor the executor: the caller builds the
runner it wants (that is where the tool and permission modules are loaded) and passes it in.
"""

from __future__ import annotations

from typing import Any

from velune.council.ports import IdSource
from velune.orchestration.engine.artifacts import AssignmentPolicy
from velune.orchestration.engine.driver import ActingPort, PlanReviewDriver, Validator, non_blank
from velune.orchestration.engine.errors import EngineDisabledError
from velune.orchestration.engine.execcontrol import ExecutionBudget
from velune.orchestration.engine.flag import engine_enabled
from velune.orchestration.engine.registry import AgentRegistry
from velune.orchestration.engine.runner import AgentRunner


def create_plan_review_driver(
    config: Any,
    registry: AgentRegistry,
    runner: AgentRunner,
    ids: IdSource,
    *,
    max_attempts: int = 3,
    validator: Validator = non_blank,
    assignments: AssignmentPolicy | None = None,
    acting_runner: ActingPort | None = None,
    execution_budget: ExecutionBudget | None = None,
) -> PlanReviewDriver:
    """Build a driver, or raise :class:`EngineDisabledError` if the flag is off."""
    if not engine_enabled(config):
        raise EngineDisabledError
    return PlanReviewDriver(
        registry,
        runner,
        ids,
        max_attempts=max_attempts,
        validator=validator,
        assignments=assignments,
        acting_runner=acting_runner,
        execution_budget=execution_budget,
    )


__all__ = ["EngineDisabledError", "create_plan_review_driver"]
