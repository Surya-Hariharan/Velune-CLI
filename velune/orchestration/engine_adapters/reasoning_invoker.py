"""``ReasoningInvoker`` over the provider acting adapter: one tool-less completion per request.

The planner and reviewer of ``PlanReviewDriver`` run through ``ReasoningAgentRunner``, which needs a
``ReasoningInvoker``. This is that invoker. It adds no provider code of its own: it asks an existing
:class:`~velune.orchestration.engine_adapters.provider_acting.ProviderActingInvoker` for one reply with
**no tools offered** and converts the result.

Because it delegates, everything below is inherited, not re-implemented:

* single retry owner (the registry's ``RetryingProvider`` layer is stripped there), bounded attempts
  and one shared deadline (``request.timeout_s``);
* the provider-wide concurrency gate;
* the same scrubber on provider-bound text, on a refused-if-changed system prompt, and on the reply;
* request and response size limits, refusals, truncation and unknown stop reasons as failures;
* no fallback to another provider or model. The provider and model are fixed by whoever builds the
  wrapped invoker, never by the request (a request only names a routing role).

What this class adds, and nothing else:

* ``offered_tools`` is always ``()``. There is no parameter, option or request field that can change
  it, so a reasoning call never carries tool definitions. A reply that contains a tool call is a
  failure (no tool was offered, so it is rejected by the wrapped invoker), and a result carrying
  proposals is rejected here as a second check.
* without ``limits`` it never sets ``attempt_gate`` and counts nothing (the Stage A behaviour). With
  ``limits`` (a view of the run's one control) every call first reserves a model call, every provider
  request reserves one attempt on the run's ledger just before it is sent, and the call's time limit
  is clamped to what remains of the run's wall clock. There is no second counter: the ledger is the
  only one, and ``ReasoningResult.attempts`` merely reports what was reserved.
* it owns no cancellation state. ``asyncio.CancelledError`` propagates unchanged and, with ``limits``,
  a cancelled run starts no further request. The deadline is ``request.timeout_s`` (clamped as above),
  enforced by the wrapped invoker; a run whose clock has run out gets a TIMEOUT result with a fixed
  message and no request. Cancelling the run stays with the caller's run control.

Failures carry a stable kind and a fixed message from the wrapped adapter (or one fixed message
defined here). Provider text, exception text and credentials are never copied into a failure.

Not here, on purpose: prompts, composition with the driver, the feature flag. The only importer is
``native_session`` (the opt-in ``velune native --plan-review`` path).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from velune.orchestration.engine.ports import (
    ActingInvoker,
    ActingRequest,
    ActingResult,
    ReasoningRequest,
    ReasoningResult,
    SeatStatus,
)
from velune.orchestration.engine_adapters.provider_acting import ProviderActingInvoker

UNUSABLE_REPLY = "the model returned no usable text"
INVOKER_FAILED = "the reasoning call failed"
DEADLINE_REACHED = "the run's time limit was reached"
BUDGET_SPENT = "the run's request budget is spent"


@runtime_checkable
class ReasoningLimits(Protocol):
    """The run's limits, seen from a reasoning call. Implemented by a view of the run's one control
    (``engine.limits.ControlLimits``); this module imports no run-control code."""

    def begin(self) -> str | None:
        """Raise ``CancelledError`` if the run was cancelled; else reserve one model call and return
        a refusal reason (``deadline`` or a budget name), or ``None`` if the call may be made."""
        ...

    def attempt_gate(self) -> Any: ...

    def remaining_s(self) -> float: ...

    def cancelled(self) -> bool: ...


class ProviderReasoningInvoker:
    """``ReasoningInvoker`` that never offers a tool. See the module docstring."""

    def __init__(self, acting: ActingInvoker, *, limits: ReasoningLimits | None = None) -> None:
        if not isinstance(acting, ActingInvoker):
            raise TypeError("a reasoning invoker wraps an acting invoker (it needs .propose)")
        if limits is not None and not isinstance(limits, ReasoningLimits):
            raise TypeError("limits must provide begin, attempt_gate, remaining_s and cancelled")
        self._acting = acting
        self._limits = limits

    @classmethod
    def from_provider(
        cls,
        provider: Any,
        model_id: str,
        *,
        limits: ReasoningLimits | None = None,
        **options: Any,
    ) -> ProviderReasoningInvoker:
        """Wrap ``provider`` with an empty tool catalog. ``options`` go to the acting invoker
        (``scrub``, ``retry``, size limits, ``clock``, ``sleep``)."""
        empty: Mapping[str, Any] = {}
        return cls(ProviderActingInvoker(provider, model_id, empty, **options), limits=limits)

    async def invoke(self, request: ReasoningRequest) -> ReasoningResult:
        """One tool-less completion. Returns a typed result; raises only on cancellation."""
        limits = self._limits
        gate: Any = None
        timeout_s = None
        if limits is not None:
            refusal = limits.begin()  # raises CancelledError if the run was cancelled
            if refusal is not None:  # no provider request is made
                if refusal == "deadline":
                    return ReasoningResult.failure(SeatStatus.TIMEOUT, DEADLINE_REACHED)
                return ReasoningResult.failure(SeatStatus.SKIPPED, BUDGET_SPENT)
            gate = limits.attempt_gate()
            # A call may not outlive the run's wall clock (the floor only keeps the value valid).
            timeout_s = max(limits.remaining_s(), 0.001)
        try:
            fields = {name: getattr(request, name) for name in ReasoningRequest.model_fields}
            if timeout_s is not None:
                fields["timeout_s"] = min(fields["timeout_s"], timeout_s)
            acting_request = ActingRequest(**fields, offered_tools=(), attempt_gate=gate)
        except Exception:  # not a well-formed ReasoningRequest
            return ReasoningResult.failure(SeatStatus.UNPARSEABLE, INVOKER_FAILED)
        try:
            result = await self._acting.propose(acting_request)
        except asyncio.CancelledError:
            raise
        except Exception:  # the wrapped invoker broke its never-raises contract
            return ReasoningResult.failure(SeatStatus.PROVIDER_ERROR, INVOKER_FAILED)
        if limits is not None and (limits.cancelled() or result.status is SeatStatus.CANCELLED):
            raise asyncio.CancelledError  # cancellation wins; nothing further starts
        if gate is not None and getattr(gate, "denied", False) and not result.ok:
            return ReasoningResult.failure(SeatStatus.SKIPPED, BUDGET_SPENT)  # no request was sent
        return _convert(result)


def _convert(result: ActingResult) -> ReasoningResult:
    extra: dict[str, Any] = {"attempts": result.attempts}
    if result.model is not None:
        extra["model"] = result.model
    if not result.ok:
        message = result.error.message if result.error is not None else ""
        return ReasoningResult.failure(result.status, message, **extra)
    if result.proposals or not (result.text or "").strip():
        # No tool was offered, so a proposal is a defect, and a text-less reply is not an answer.
        return ReasoningResult.failure(SeatStatus.UNPARSEABLE, UNUSABLE_REPLY, **extra)
    return ReasoningResult.success(result.text or "", **extra)


__all__ = [
    "BUDGET_SPENT",
    "DEADLINE_REACHED",
    "INVOKER_FAILED",
    "UNUSABLE_REPLY",
    "ProviderReasoningInvoker",
    "ReasoningLimits",
]
