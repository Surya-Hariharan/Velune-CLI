"""Request-scoped execution tracing for the Reasoning Council.

Answers one question with evidence: *why did one user request produce N
provider calls?*

Before this module the only per-run identity was ``velune.core.trace``'s
``run_id``/``agent_id`` contextvars, which feed log-line prefixes and nothing
else. ``agent_id`` carries the :class:`CouncilRole`, not the seat — the four
critics are all constructed on the REVIEWER/CHALLENGER role descriptors, so a
STANDARD run logged four separate "agent=reviewer" inferences with no way to
tell the reviewer from the security, performance, and maintainability critics.
The trace was therefore unable to explain its own call count.

Two record types make the run explainable:

- :class:`ProviderCall` — one physical ``stream()``/``infer()`` invocation,
  attributed to the seat that caused it, carrying *why* it happened
  (:class:`CallReason`) so a repeat execution is never silently unexplained.
- :class:`ExecutionNode` — one orchestration phase (classification, planning,
  seat execution, review, debate, arbitration, synthesis), owning the provider
  calls made beneath it.

Nothing here records prompts, messages, completions, or credentials — only
structural metadata (seat, provider, model id, reason, timing, outcome).
"""

from __future__ import annotations

import contextvars
import time
import uuid
from dataclasses import dataclass, field

from velune._compat import StrEnum

# The active trace for the request being served. Council execution happens
# across asyncio tasks spawned by the scheduler, which inherit the context at
# creation time, so a trace set at the top of a request is visible to every
# seat beneath it without threading an argument through every call site.
_active_trace: contextvars.ContextVar[RequestTrace | None] = contextvars.ContextVar(
    "velune_request_trace", default=None
)
_active_seat: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "velune_active_seat", default=None
)
_active_node: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "velune_active_node", default=None
)


class CallReason(StrEnum):
    """Why a provider call happened.

    Every provider call carries one. A seat executing more than once must
    attribute each repeat to a reason other than ``PRIMARY`` — that is the
    invariant :func:`RequestTrace.unexplained_calls` enforces.
    """

    PRIMARY = "primary"  # the seat's first, contract-sanctioned execution
    SELF_CONSISTENCY = "self_consistency"  # extra Coder sample in the diverge round
    REVISION = "revision"  # re-execution driven by critic feedback (debate)
    RETRY = "retry"  # same request re-sent after a transient failure
    FALLBACK = "fallback"  # re-sent to a *different* provider after failure
    VALIDATION_FAILURE = "validation_failure"  # output failed schema validation
    ESCALATION = "escalation"  # tier escalated mid-run


class NodeType(StrEnum):
    """Phases of the request execution graph."""

    REQUEST = "request"
    CLASSIFICATION = "classification"
    CONTEXT = "context"
    PLANNING = "planning"
    SEAT_EXECUTION = "seat_execution"
    REVIEW = "review"
    DEBATE = "debate"
    ARBITRATION = "arbitration"
    SYNTHESIS = "synthesis"


@dataclass
class ProviderCall:
    """One physical provider invocation, attributed to its cause."""

    call_id: str
    request_id: str
    seat: str
    component: str
    provider: str
    model: str
    purpose: str
    reason: CallReason
    parent_call_id: str | None = None
    node_id: str | None = None
    streaming: bool = True
    attempt: int = 1
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    status: str = "running"  # running | ok | error | timeout
    error_type: str | None = None

    @property
    def duration_ms(self) -> float:
        end = self.completed_at if self.completed_at is not None else time.time()
        return (end - self.started_at) * 1000.0

    def finish(self, status: str, error_type: str | None = None) -> None:
        self.completed_at = time.time()
        self.status = status
        self.error_type = error_type


@dataclass
class ExecutionNode:
    """One orchestration phase in the request execution graph."""

    node_id: str
    parent_id: str | None
    type: NodeType
    label: str
    status: str = "running"  # running | ok | skipped | error
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    provider_call_ids: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def duration_ms(self) -> float:
        end = self.completed_at if self.completed_at is not None else time.time()
        return (end - self.started_at) * 1000.0

    def finish(self, status: str = "ok", detail: str = "") -> None:
        self.completed_at = time.time()
        self.status = status
        if detail:
            self.detail = detail


class RequestTrace:
    """The execution graph and provider-call ledger for a single user request."""

    def __init__(self, request_id: str | None = None, prompt_preview: str = "") -> None:
        self.request_id = request_id or f"REQ-{uuid.uuid4().hex[:8]}"
        # A short, redacted preview only — never the full prompt, which may
        # carry file contents or anything else the user typed.
        self.prompt_preview = prompt_preview[:60]
        self.started_at = time.time()
        self.tier: str | None = None
        self.contract_summary: str = ""
        self.calls: list[ProviderCall] = []
        self.nodes: list[ExecutionNode] = []
        self._root = ExecutionNode(
            node_id="n0", parent_id=None, type=NodeType.REQUEST, label="user request"
        )
        self.nodes.append(self._root)

    # ── graph construction ────────────────────────────────────────────────

    def open_node(self, type: NodeType, label: str, parent_id: str | None = None) -> ExecutionNode:
        node = ExecutionNode(
            node_id=f"n{len(self.nodes)}",
            parent_id=parent_id or _active_node.get() or self._root.node_id,
            type=type,
            label=label,
        )
        self.nodes.append(node)
        return node

    def open_call(
        self,
        *,
        seat: str,
        component: str,
        provider: str,
        model: str,
        purpose: str,
        reason: CallReason = CallReason.PRIMARY,
        attempt: int = 1,
        streaming: bool = True,
        parent_call_id: str | None = None,
    ) -> ProviderCall:
        call = ProviderCall(
            call_id=f"{self.request_id}-c{len(self.calls) + 1}",
            request_id=self.request_id,
            seat=seat,
            component=component,
            provider=provider,
            model=model,
            purpose=purpose,
            reason=reason,
            attempt=attempt,
            streaming=streaming,
            parent_call_id=parent_call_id,
            node_id=_active_node.get(),
        )
        self.calls.append(call)
        node_id = call.node_id
        if node_id:
            for node in self.nodes:
                if node.node_id == node_id:
                    node.provider_call_ids.append(call.call_id)
                    break
        return call

    # ── reporting ─────────────────────────────────────────────────────────

    def calls_by_seat(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for call in self.calls:
            counts[call.seat] = counts.get(call.seat, 0) + 1
        return counts

    def unexplained_calls(self) -> list[ProviderCall]:
        """Return calls that repeat a seat without giving a reason for the repeat.

        A seat's first ``PRIMARY`` call is self-explanatory. Every *additional*
        call by that seat must carry a non-``PRIMARY`` reason; if it does not,
        the run performed a silent repeated deliberation, which is exactly the
        failure mode this ledger exists to make impossible.
        """
        seen: set[str] = set()
        offenders: list[ProviderCall] = []
        for call in self.calls:
            if call.seat in seen and call.reason is CallReason.PRIMARY:
                offenders.append(call)
            seen.add(call.seat)
        return offenders

    def render(self) -> str:
        """Human-readable execution trace. Structural metadata only."""
        lines = [
            f"Request {self.request_id}  tier={self.tier or 'n/a'}  "
            f"calls={len(self.calls)}  {(time.time() - self.started_at) * 1000:.0f}ms"
        ]
        if self.contract_summary:
            lines.append(f"  contract: {self.contract_summary}")

        children: dict[str | None, list[ExecutionNode]] = {}
        for node in self.nodes:
            children.setdefault(node.parent_id, []).append(node)
        by_id = {c.call_id: c for c in self.calls}

        def walk(node: ExecutionNode, depth: int) -> None:
            pad = "  " * (depth + 1)
            lines.append(f"{pad}├── {node.label} [{node.status}] {node.duration_ms:.0f}ms")
            for call_id in node.provider_call_ids:
                call = by_id.get(call_id)
                if call is None:
                    continue
                lines.append(
                    f"{pad}    └── {call.call_id}  seat={call.seat}  "
                    f"{call.provider}/{call.model}  reason={call.reason.value}  "
                    f"[{call.status}] {call.duration_ms:.0f}ms"
                )
            for child in children.get(node.node_id, []):
                walk(child, depth + 1)

        for child in children.get(self._root.node_id, []):
            walk(child, 0)

        unexplained = self.unexplained_calls()
        if unexplained:
            lines.append(
                f"  WARNING: {len(unexplained)} unexplained repeat call(s): "
                + ", ".join(c.call_id for c in unexplained)
            )
        else:
            lines.append("  All provider calls accounted for.")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "tier": self.tier,
            "contract": self.contract_summary,
            "total_provider_calls": len(self.calls),
            "calls_by_seat": self.calls_by_seat(),
            "unexplained_calls": [c.call_id for c in self.unexplained_calls()],
            "nodes": [
                {
                    "node_id": n.node_id,
                    "parent_id": n.parent_id,
                    "type": n.type.value,
                    "label": n.label,
                    "status": n.status,
                    "duration_ms": round(n.duration_ms, 1),
                    "provider_call_ids": list(n.provider_call_ids),
                }
                for n in self.nodes
            ],
            "calls": [
                {
                    "call_id": c.call_id,
                    "parent_call_id": c.parent_call_id,
                    "node_id": c.node_id,
                    "seat": c.seat,
                    "component": c.component,
                    "provider": c.provider,
                    "model": c.model,
                    "purpose": c.purpose,
                    "reason": c.reason.value,
                    "attempt": c.attempt,
                    "streaming": c.streaming,
                    "status": c.status,
                    "duration_ms": round(c.duration_ms, 1),
                    "error_type": c.error_type,
                }
                for c in self.calls
            ],
        }


# ── context helpers ───────────────────────────────────────────────────────


def current_trace() -> RequestTrace | None:
    """Return the trace for the in-flight request, if one is active."""
    return _active_trace.get()


def current_seat() -> str | None:
    """Return the seat name currently executing, if any."""
    return _active_seat.get()


class trace_request:
    """Context manager establishing the request-scoped trace and correlation id."""

    def __init__(self, request_id: str | None = None, prompt_preview: str = "") -> None:
        self.trace = RequestTrace(request_id, prompt_preview)
        self._token: contextvars.Token | None = None

    def __enter__(self) -> RequestTrace:
        self._token = _active_trace.set(self.trace)
        return self.trace

    def __exit__(self, *exc: object) -> None:
        if self._token is not None:
            _active_trace.reset(self._token)


class trace_node:
    """Context manager opening (and closing) one execution-graph node."""

    def __init__(self, type: NodeType, label: str) -> None:
        self.type = type
        self.label = label
        self.node: ExecutionNode | None = None
        self._token: contextvars.Token | None = None

    def __enter__(self) -> ExecutionNode | None:
        trace = current_trace()
        if trace is None:
            return None
        self.node = trace.open_node(self.type, self.label)
        self._token = _active_node.set(self.node.node_id)
        return self.node

    def __exit__(self, exc_type: type | None, *_: object) -> None:
        if self._token is not None:
            _active_node.reset(self._token)
        if self.node is not None and self.node.status == "running":
            self.node.finish("error" if exc_type else "ok")


class trace_seat:
    """Context manager naming the seat responsible for calls made inside it.

    Seat identity is what :class:`CouncilRole` cannot supply: the security,
    performance, and maintainability critics all run on the REVIEWER role
    descriptor, so only an explicit seat name distinguishes their calls in the
    ledger.
    """

    def __init__(self, seat: str, reason: CallReason = CallReason.PRIMARY) -> None:
        self.seat = seat
        self.reason = reason
        self._seat_token: contextvars.Token | None = None
        self._reason_token: contextvars.Token | None = None

    def __enter__(self) -> trace_seat:
        self._seat_token = _active_seat.set(self.seat)
        self._reason_token = _active_reason.set(self.reason)
        return self

    def __exit__(self, *_: object) -> None:
        if self._seat_token is not None:
            _active_seat.reset(self._seat_token)
        if self._reason_token is not None:
            _active_reason.reset(self._reason_token)


_active_reason: contextvars.ContextVar[CallReason] = contextvars.ContextVar(
    "velune_call_reason", default=CallReason.PRIMARY
)


def current_reason() -> CallReason:
    """Return the reason attributed to calls made in the current scope."""
    return _active_reason.get()
