"""The run's limits, as seen by a reasoning call. A view of the run's one :class:`RunControl`.

A plan/review run has exactly one ``RunControl``: its wall clock, its model-call and provider-attempt
budget and its cancellation token. The acting runner receives that control directly. A reasoning call
(planner, reviewer) has no control parameter in its port, so the invoker is handed this view of the
*same* control when it is built. The view owns nothing:

* ``begin()`` raises ``CancelledError`` if the run was cancelled, and otherwise reserves one model call
  on the run's ledger (which also refuses once the deadline has passed or a budget is spent);
* ``attempt_gate()`` hands out the same per-call gate the acting runner uses, so each provider request
  reserves one attempt on the run's ledger immediately before it is sent;
* ``remaining_s()`` is the time left on the run's wall clock.

There is no second counter, deadline or cancellation flag here.
"""

from __future__ import annotations

import asyncio

from velune.orchestration.engine.acting import AttemptGate
from velune.orchestration.engine.execcontrol import RunControl


class ControlLimits:
    """A reasoning invoker's window onto the run's single control."""

    def __init__(self, control: RunControl) -> None:
        self._control = control

    @property
    def run_id(self) -> str:
        return self._control.run_id

    def begin(self) -> str | None:
        """Refusal reason (``deadline``, ``model_calls``, ``provider_attempts``) or ``None``."""
        if self._control.token.cancelled:
            raise asyncio.CancelledError
        refusal = self._control.budget.reserve_model_call()
        return refusal.value if refusal is not None else None

    def attempt_gate(self) -> AttemptGate:
        return AttemptGate(self._control.budget)

    def remaining_s(self) -> float:
        return self._control.budget.remaining_s()

    def cancelled(self) -> bool:
        return self._control.token.cancelled


__all__ = ["ControlLimits"]
