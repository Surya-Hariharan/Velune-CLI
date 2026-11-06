"""Approval of one specific tool invocation. Not a permission, not a grant, not a policy.

An approval answers exactly one question: "may *this* invocation (this tool, these canonical
arguments, this invocation identity) run, once?" It is bound to the tool name, the ``call_digest``
from Phase 4D-1 and a unique invocation id minted by trusted code. It is single-use, expires, and
cannot be moved to another tool, another argument set or another invocation.

There is deliberately no "always allow", no session grant, no tool-name-only approval and no AUTO
mode here. Approval never replaces authorization (the permission gate) and authorization never
replaces approval: the executor requires both.

Everything is synchronous bookkeeping around one async port, so a check-and-consume has no ``await``
in it and cannot interleave with another consume on the same event loop.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from pydantic import Field

from velune._compat import StrEnum
from velune.council.serialization import Contract

DEFAULT_APPROVAL_TTL_S = 120.0


class InvocationRequest(Contract):
    """The thing an approver is asked about. Minted by the ledger, never by the model."""

    invocation_id: str = Field(min_length=8, max_length=64)
    tool: str
    call_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Presentation only: sanitized, redacted, bounded text for a human. Never compared in
    # ``consume`` and never an input to authorization.
    display: str = Field(default="", max_length=2000)


class ApprovalDecision(Contract):
    """An approver's answer, echoing the exact invocation it is about."""

    invocation_id: str
    tool: str
    call_digest: str
    approved: bool


class ApprovalFailure(StrEnum):
    """Why an approval could not be consumed. Stable, value-free."""

    MISSING = "missing"
    DENIED = "denied"
    STALE = "stale"  # expired, unknown, or already consumed
    MISMATCH = "mismatch"  # bound to a different tool, digest or invocation


@runtime_checkable
class ApprovalPort(Protocol):
    """Asks for a decision on one invocation. A port that errors or returns nothing means *no*."""

    async def request(self, invocation: InvocationRequest) -> ApprovalDecision | None: ...


class HeadlessApprovalPort:
    """Denies everything. The default: nothing is approved because nobody was asked."""

    async def request(self, invocation: InvocationRequest) -> ApprovalDecision | None:
        return ApprovalDecision(
            invocation_id=invocation.invocation_id,
            tool=invocation.tool,
            call_digest=invocation.call_digest,
            approved=False,
        )


class ApprovalLedger:
    """Tracks invocations that are awaiting a decision, and consumes decisions exactly once.

    Only pending invocations can be approved. An invocation leaves ``pending`` when it is consumed,
    when it expires, or when :meth:`discard` is called, so an old decision can never match again.
    """

    def __init__(
        self,
        *,
        ttl_s: float = DEFAULT_APPROVAL_TTL_S,
        clock: Callable[[], float] = time.monotonic,
        new_id: Callable[[], str] | None = None,
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        self._ttl_s = ttl_s
        self._clock = clock
        self._new_id = new_id or (lambda: uuid.uuid4().hex)
        self._pending: dict[str, tuple[InvocationRequest, float]] = {}

    def issue(self, tool: str, call_digest: str, display: str = "") -> InvocationRequest:
        """Mint a fresh invocation identity and mark it pending."""
        invocation_id = self._new_id()
        if invocation_id in self._pending:  # never reuse a live identity
            raise RuntimeError("invocation id collision")
        request = InvocationRequest(
            invocation_id=invocation_id, tool=tool, call_digest=call_digest, display=display
        )
        self._pending[invocation_id] = (request, self._clock())
        return request

    def consume(self, decision: ApprovalDecision | None) -> ApprovalFailure | None:
        """Use a decision once. Returns ``None`` only when the invocation is approved and consumed."""
        if decision is None:
            return ApprovalFailure.MISSING
        entry = self._pending.pop(decision.invocation_id, None)  # removed first: single use
        if entry is None:
            return ApprovalFailure.STALE
        request, issued_at = entry
        if self._clock() - issued_at > self._ttl_s:
            return ApprovalFailure.STALE
        if decision.tool != request.tool or decision.call_digest != request.call_digest:
            return ApprovalFailure.MISMATCH
        if not decision.approved:
            return ApprovalFailure.DENIED
        return None

    def discard(self, invocation_id: str) -> None:
        self._pending.pop(invocation_id, None)

    @property
    def pending_count(self) -> int:
        return len(self._pending)
