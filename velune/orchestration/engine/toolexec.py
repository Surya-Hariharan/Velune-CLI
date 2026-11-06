"""The trusted tool executor: the only engine code that may cause a tool to run.

It takes a :class:`ValidatedToolCall` and a :class:`ResolvedToolPolicy` (both from Phase 4D-1) and
runs the call only if *every* gate holds, in this order:

1. the call is well formed (its digests recompute) and the tool is inside the resolved policy and the
   fixed read-only ceiling;
2. the tool is looked up in the trusted registry *now*, and is still the tool that was validated
   (same name, same declared scopes, arguments still satisfy its live schema);
3. a unique invocation identity is minted and exactly that invocation is put to the approval port;
4. the approval is consumed once (bound to tool, ``call_digest`` and invocation id);
5. the call goes through ``authorize_and_execute``, the existing enforced entry point (scope check,
   permission gate, PreToolUse/PostToolUse hooks), under a *headless* gate that has no way to ask a
   user and no grants, so anything the policy would ask about is refused.

This module never calls ``tool.execute`` or ``guarded_execute``; a test enforces that. Failures are
typed and value-free: no raw exception text is returned. Tool output is data a tool produced: it is
clipped, passed through ``redact_secrets`` (best effort, pattern based), and always labelled
``origin=tool`` and ``trusted=False``. It is not stored anywhere and no driver uses this yet.

Phase 4D-3 adds optional per-run control (``execcontrol.RunControl``): budgets, idempotency and
cancellation. Without a ``control`` the executor behaves exactly as in 4D-2. With one:

* the call is *claimed* by a trusted ``request_id`` (bound to run, tool and ``call_digest``), so the
  same request is never run twice and a changed call under the same id is rejected;
* budgets are checked before approval is requested and *reserved* atomically after it is consumed, so a
  denied approval costs nothing but any started call (even one that fails) counts once;
* a started call is awaited but **never cancelled or interrupted**. If cancellation is requested while
  it runs, the executor stops waiting and reports that it may still be running or may have completed;
* the wall-clock limit is cooperative: checked before the call and after it returns.

Retry rule (nothing here retries anything): a call is safe to retry only when trusted code knows it
never began (``safe_to_retry``). A call that was entered, that overran, or whose outcome is unknown is
never marked safe, and its recorded outcome is returned on a duplicate request instead of re-running.
No exactly-once side-effect guarantee is claimed.

What proves a tool "never began" (Phase 4D-3 review fix): **exception type never does.**
``ActionDeniedError``/``ToolPermissionError`` are raised by ``authorize_and_execute`` *before* the tool
body, but a tool body may raise the very same classes, and ``ToolBlockedError`` is raised from inside
``guarded_execute``. The existing API puts no marker on either. The only trusted fact available is
control flow that this module owns: the per-call gate is a :class:`_ProbedGate` that records when its
``check`` has returned. ``authorize_and_execute`` runs scope check, ``describe_actions`` and that check
strictly before it enters ``guarded_execute``, so a denial raised while the check had *not* returned
provably happened before the body. Anything raised after the check returned may have come from the
body, a hook or ``authorize_and_execute`` itself, and is classified as possibly executed.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from velune._compat import StrEnum
from velune.core.redaction import redact_secrets
from velune.council.serialization import Contract
from velune.orchestration.engine.approval import (
    ApprovalFailure,
    ApprovalLedger,
    ApprovalPort,
    HeadlessApprovalPort,
)
from velune.orchestration.engine.artifacts import ArtifactOrigin
from velune.orchestration.engine.execcontrol import (
    BudgetRefusal,
    Claim,
    ClaimKind,
    InvocationState,
    RunControl,
)
from velune.orchestration.engine.toolpolicy import (
    EXCLUDED_TOOLS,
    READ_ONLY_PERMISSIONS,
    READ_ONLY_TOOL_CEILING,
    ResolvedToolPolicy,
)
from velune.orchestration.engine.toolschema import ArgumentError, check_arguments, schema_problem
from velune.orchestration.engine.toolvalidate import (
    ValidatedToolCall,
    argument_digest,
    call_digest,
)
from velune.permissions.boundary import Boundary
from velune.permissions.gate import ActionDeniedError, PermissionGate
from velune.permissions.policy import ExecutionMode, PolicyState
from velune.tools.base.tool import (
    ToolCallContext,
    ToolPermission,
    ToolPermissionError,
    authorize_and_execute,
)


@dataclass
class _ProbedGate(PermissionGate):
    """The headless gate, plus a record of whether its check has returned (see module docstring).

    ``check_passed`` flips only after the real ``check`` returned normally, i.e. the call is about to
    enter ``guarded_execute``. It never flips back, so it can only make classification *more*
    conservative (a tool body that calls the gate itself can set it, never clear it).
    """

    check_passed: bool = False

    async def check(self, tool_name: str, actions: list[Any]) -> list[Path]:
        approved = await super().check(tool_name, actions)
        self.check_passed = True
        return approved


def _denied_before_entry(exc: BaseException, ctx: ToolCallContext) -> bool:
    """True only when a permission denial is provably from before the tool body could be entered."""
    gate = ctx.gate
    return (
        isinstance(exc, ActionDeniedError | ToolPermissionError)
        and isinstance(gate, _ProbedGate)
        and not gate.check_passed
    )


# Output bound. Redaction runs over the complete output first; only the sanitized text is clipped to
# this public limit, so a secret can never be cut in half by the clip.
MAX_OUTPUT_CHARS = 20_000
SCRUB_FAILED_TEXT = "[output withheld: the secret scrubber failed]"
TRUNCATION_MARKER = "\n[output truncated by the engine]"

_ACTOR = "engine.toolexec"


class ToolStatus(StrEnum):
    OK = "ok"
    REFUSED = "refused"  # never reached the tool
    FAILED = "failed"  # the tool ran (or was entered) and did not deliver


class ToolErrorCategory(StrEnum):
    """Safe, fixed categories. Nothing here is derived from tool output or exception text."""

    MALFORMED = "malformed"
    NOT_ALLOWED = "not_allowed"
    UNKNOWN_TOOL = "unknown_tool"
    TOOL_CHANGED = "tool_changed"
    APPROVAL_MISSING = "approval_missing"
    APPROVAL_DENIED = "approval_denied"
    APPROVAL_STALE = "approval_stale"
    APPROVAL_MISMATCH = "approval_mismatch"
    AUTHORIZATION_DENIED = "authorization_denied"
    # No longer produced: a hook block is indistinguishable from a tool-body error (see docstring).
    HOOK_BLOCKED = "hook_blocked"
    TOOL_ERROR = "tool_error"
    INTERNAL = "internal"
    BUDGET_DEADLINE = "budget_deadline"
    BUDGET_CALLS = "budget_calls"
    BUDGET_ARGUMENT_BYTES = "budget_argument_bytes"
    BUDGET_RESULT_BYTES = "budget_result_bytes"
    BUDGET_CONCURRENCY = "budget_concurrency"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    CANCELLED = "cancelled"
    DUPLICATE_PENDING = "duplicate_pending"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ToolOutcome(StrEnum):
    """What happened to the *operation*, separate from ``ToolStatus``. Conservative by design."""

    COMPLETED = "completed"
    FAILED_BEFORE_EXECUTION = "failed_before_execution"  # trusted code knows the tool never began
    FAILED_DURING_EXECUTION = "failed_during_execution"  # the tool was entered and raised
    DEADLINE_EXCEEDED = "deadline_exceeded"  # it returned, but after the deadline
    CANCELLED_BEFORE_EXECUTION = "cancelled_before_execution"
    # Cancellation was requested while it ran; it was not interrupted and may still be running.
    CANCEL_REQUESTED_DURING_EXECUTION = "cancel_requested_during_execution"
    OUTCOME_UNKNOWN = "outcome_unknown"


_BUDGET_CATEGORY: dict[BudgetRefusal, ToolErrorCategory] = {
    BudgetRefusal.DEADLINE: ToolErrorCategory.BUDGET_DEADLINE,
    BudgetRefusal.CALLS: ToolErrorCategory.BUDGET_CALLS,
    BudgetRefusal.ARGUMENT_BYTES: ToolErrorCategory.BUDGET_ARGUMENT_BYTES,
    BudgetRefusal.RESULT_BYTES: ToolErrorCategory.BUDGET_RESULT_BYTES,
    BudgetRefusal.CONCURRENCY: ToolErrorCategory.BUDGET_CONCURRENCY,
}
# Refused before starting, yet a plain retry is still not "safe": the original may be running.
_NEVER_SAFE_TO_RETRY = frozenset(
    {ToolErrorCategory.DUPLICATE_PENDING, ToolErrorCategory.IDEMPOTENCY_CONFLICT}
)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


_APPROVAL_CATEGORY: dict[ApprovalFailure, ToolErrorCategory] = {
    ApprovalFailure.MISSING: ToolErrorCategory.APPROVAL_MISSING,
    ApprovalFailure.DENIED: ToolErrorCategory.APPROVAL_DENIED,
    ApprovalFailure.STALE: ToolErrorCategory.APPROVAL_STALE,
    ApprovalFailure.MISMATCH: ToolErrorCategory.APPROVAL_MISMATCH,
}


class ToolExecutionResult(Contract):
    """What running (or refusing) one invocation produced. Suitable for a later tool artifact.

    ``output`` is untrusted tool-origin data. ``origin`` and ``trusted`` are fixed by the type, so
    text a tool returns cannot claim another origin, an approval or an engine instruction: it can
    only ever occupy the ``output`` field.
    """

    invocation_id: str
    call_id: str
    tool: str
    call_digest: str
    status: ToolStatus
    error: ToolErrorCategory | None = None
    output: str = ""
    truncated: bool = False
    original_chars: int = Field(default=0, ge=0)
    origin: Literal[ArtifactOrigin.TOOL] = ArtifactOrigin.TOOL
    trusted: Literal[False] = False
    # Phase 4D-3. ``executed`` means the tool may have been entered; if True the operation may have
    # had effects, whatever ``status`` says. ``safe_to_retry`` is True only when trusted code knows
    # it never began. ``replayed`` marks a recorded outcome returned for a duplicate request.
    request_id: str = ""
    outcome: ToolOutcome = ToolOutcome.COMPLETED
    executed: bool = False
    cancel_requested: bool = False
    replayed: bool = False
    safe_to_retry: bool = False

    @property
    def ok(self) -> bool:
        return self.status is ToolStatus.OK


def _as_text(raw: Any) -> str:
    """Deterministic text for a tool's return value, never its ``repr`` or an exception message."""
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw, default=lambda o: f"<{type(o).__name__}>", ensure_ascii=False)
    except Exception:
        return f"[unserializable {type(raw).__name__} output]"


def bound_output(raw: Any, scrub: Callable[[str], str] | None = None) -> tuple[str, bool, int]:
    """Redact the complete output, then cap it. Returns ``(text, truncated, original_chars)``.

    Order: (1) render to text, (2) ``redact_secrets`` over the *whole* text, (3) clip the sanitized
    text to ``MAX_OUTPUT_CHARS``. ``original_chars`` is the length of the sanitized text before the
    clip (not the raw length, which redaction can change and which would reveal a secret's size), so
    ``truncated`` is exactly ``original_chars > MAX_OUTPUT_CHARS``. The returned text never exceeds
    ``MAX_OUTPUT_CHARS``.

    Redaction is the existing pattern-based ``redact_secrets``: it catches known key shapes and
    configured provider env values, not arbitrary secrets. It reduces exposure; it is not a
    guarantee.
    """
    text = redact_secrets(_as_text(raw))
    if scrub is not None:
        # Run-specific scrubbing (for example the configured provider keys) happens *before* the clip,
        # so a secret cannot be split by the cut and left half-visible. A scrubber that fails
        # withholds the output instead of passing it through.
        try:
            text = scrub(text)
        except Exception:
            text = SCRUB_FAILED_TEXT
    original = len(text)
    if original <= MAX_OUTPUT_CHARS:
        return text, False, original
    keep = MAX_OUTPUT_CHARS - len(TRUNCATION_MARKER)
    return text[:keep] + TRUNCATION_MARKER, True, original


def _clip_bytes(text: str, limit: int) -> tuple[str, bool]:
    """Clip to at most *limit* UTF-8 bytes without splitting a character."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text, False
    return data[:limit].decode("utf-8", errors="ignore"), True


_DISPLAY_VALUE_CHARS = 200
_DISPLAY_KEY_CHARS = 64
_DISPLAY_TOTAL_CHARS = 800


def approval_display(call: ValidatedToolCall, scrub: Callable[[str], str] | None = None) -> str:
    """Bounded, redacted, escaped ``key: value`` lines for a human approver. Presentation only.

    Every value is secret-scrubbed first, then JSON-encoded with ASCII escapes (so control
    characters, ANSI sequences, newlines and look-alike Unicode show as visible escapes), then
    clipped. A clipped display says so; the approval still binds to the full-argument digest.
    """
    clean = scrub if scrub is not None else redact_secrets

    def safe(text: str) -> str:
        try:
            return clean(text)
        except Exception:
            return "[unavailable]"  # never show unscrubbed text if scrubbing fails

    def clip(text: str, limit: int) -> tuple[str, bool]:
        return (text[:limit] + "…", True) if len(text) > limit else (text, False)

    try:
        arguments = call.arguments
    except Exception:
        return "(arguments unavailable)"
    lines: list[str] = []
    clipped = False
    for key in sorted(arguments, key=str):
        value = arguments[key]
        raw = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
        shown_key, k_clip = clip(json.dumps(safe(str(key)))[1:-1], _DISPLAY_KEY_CHARS)
        shown_val, v_clip = clip(json.dumps(safe(raw)), _DISPLAY_VALUE_CHARS)
        clipped = clipped or k_clip or v_clip
        lines.append(f"{shown_key}: {shown_val}")
    text = "\n".join(lines) if lines else "(no arguments)"
    if len(text) > _DISPLAY_TOTAL_CHARS:
        text, clipped = text[:_DISPLAY_TOTAL_CHARS] + "…", True
    if clipped:
        total = len(call.arguments_json)
        text += f"\n(display truncated; {total} chars of arguments in total)"
    return text[:2000]


class TrustedToolExecutor:
    """Runs validated, policy-permitted, approved and authorized read-only tool calls.

    ``registry`` is the trusted tool registry (``get(name)``). ``approvals`` defaults to a port that
    denies everything. The permission gate is built here, per call, in MANUAL mode with no ``ask``
    callback and no grants: the REPL session gate and its persistent grants are never used.
    """

    def __init__(
        self,
        registry: Any,
        workspace: Path,
        approvals: ApprovalPort | None = None,
        *,
        ledger: ApprovalLedger | None = None,
        audit: Callable[[dict[str, Any]], None] | None = None,
        scrub: Callable[[str], str] | None = None,
    ) -> None:
        self._registry = registry
        self._workspace = Path(workspace)
        self._approvals: ApprovalPort = (
            approvals if approvals is not None else HeadlessApprovalPort()
        )
        self._ledger = ledger if ledger is not None else ApprovalLedger()
        self._audit = audit
        self._scrub = scrub

    async def execute(
        self,
        call: ValidatedToolCall,
        policy: ResolvedToolPolicy,
        *,
        run_id: str,
        control: RunControl | None = None,
        request_id: str | None = None,
    ) -> ToolExecutionResult:
        """Run one validated call, or return a typed refusal. Never raises except on cancellation.

        ``control`` and ``request_id`` go together. ``request_id`` must come from trusted engine code
        (for example a step counter), never from model output or ``call.call_id``.
        """
        if control is not None and (
            control.run_id != run_id or request_id is None or not _REQUEST_ID.match(request_id)
        ):
            return self._result(call, "", ToolStatus.REFUSED, ToolErrorCategory.MALFORMED)
        key = request_id or ""
        invocation_id = ""
        executed = False
        claimed = False
        try:
            refusal = self._precheck(call, policy)
            if refusal is not None:
                return self._result(call, "", ToolStatus.REFUSED, refusal, request_id=key)

            tool = self._registry.get(call.name)
            changed = self._tool_problem(tool, call)
            if changed is not None:
                return self._result(call, "", ToolStatus.REFUSED, changed, request_id=key)

            argument_bytes = len(call.arguments_json.encode("utf-8"))
            if control is not None:
                claim = control.store.claim(key, call.name, call.call_digest)
                if claim.kind is not ClaimKind.NEW:
                    return self._duplicate(call, claim, key)
                claimed = True
                if control.token.cancelled:
                    return self._cancelled_before(call, "", control, key)
                early = control.budget.check(argument_bytes)
                if early is not None:
                    return self._result(
                        call, "", ToolStatus.REFUSED, _BUDGET_CATEGORY[early], request_id=key
                    )

            invocation = self._ledger.issue(
                call.name, call.call_digest, approval_display(call, self._scrub)
            )
            invocation_id = invocation.invocation_id
            try:
                decision = await self._approvals.request(invocation)
            except asyncio.CancelledError:
                raise
            except Exception:
                decision = None  # an unavailable approver is a "no"
            failure = self._ledger.consume(decision)  # the approval is spent here, win or lose
            if failure is not None:
                return self._result(
                    call,
                    invocation_id,
                    ToolStatus.REFUSED,
                    _APPROVAL_CATEGORY[failure],
                    request_id=key,
                )

            if control is None:
                executed = True
                return await self._run(call, tool, invocation_id, run_id)

            if control.token.cancelled:  # the approval stays spent; nothing ran, nothing counted
                return self._cancelled_before(call, invocation_id, control, key)
            reserved = control.budget.reserve(argument_bytes)
            if isinstance(reserved, BudgetRefusal):
                return self._result(
                    call,
                    invocation_id,
                    ToolStatus.REFUSED,
                    _BUDGET_CATEGORY[reserved],
                    request_id=key,
                )
            executed = True
            return await self._run_controlled(call, tool, invocation_id, control, key, reserved)
        except asyncio.CancelledError:
            raise
        except Exception:
            return self._result(
                call,
                invocation_id,
                ToolStatus.FAILED,
                ToolErrorCategory.INTERNAL,
                outcome=(
                    ToolOutcome.OUTCOME_UNKNOWN if executed else ToolOutcome.FAILED_BEFORE_EXECUTION
                ),
                executed=executed,
                request_id=key,
            )
        finally:
            if invocation_id:
                self._ledger.discard(invocation_id)
            if (
                claimed
                and control is not None
                and control.store.state_of(key) is InvocationState.PENDING
            ):
                if executed:  # we lost track of it: never leave a possibly-run call replayable
                    control.store.settle(key, InvocationState.UNKNOWN, None)
                else:
                    control.store.release(key)

    # ── checks ────────────────────────────────────────────────────────────────

    def _precheck(
        self, call: ValidatedToolCall, policy: ResolvedToolPolicy
    ) -> ToolErrorCategory | None:
        try:
            arguments_ok = (
                argument_digest(call.arguments_json) == call.arguments_digest
                and call_digest(call.name, call.arguments_json) == call.call_digest
                and isinstance(json.loads(call.arguments_json), dict)
            )
        except (ValueError, TypeError):
            arguments_ok = False
        if not arguments_ok:
            return ToolErrorCategory.MALFORMED
        # Both the resolved policy and the fixed driver ceiling must permit it; a hand-built policy
        # cannot widen the ceiling.
        if (
            not policy.permits(call.name)
            or call.name not in READ_ONLY_TOOL_CEILING
            or call.name in EXCLUDED_TOOLS
        ):
            return ToolErrorCategory.NOT_ALLOWED
        if not call.declared_permissions or not call.declared_permissions <= READ_ONLY_PERMISSIONS:
            return ToolErrorCategory.NOT_ALLOWED
        return None

    def _tool_problem(self, tool: Any, call: ValidatedToolCall) -> ToolErrorCategory | None:
        if tool is None:
            return ToolErrorCategory.UNKNOWN_TOOL
        try:
            if tool.get_name() != call.name:
                return ToolErrorCategory.TOOL_CHANGED
            live = frozenset(str(getattr(p, "value", p)) for p in tool.get_required_permissions())
            if live != call.declared_permissions or not live <= READ_ONLY_PERMISSIONS:
                return ToolErrorCategory.TOOL_CHANGED
            schema = dict(tool.get_schema() or {})
            if schema_problem(schema):
                return ToolErrorCategory.TOOL_CHANGED
            check_arguments(schema, call.arguments)
        except ArgumentError:
            return ToolErrorCategory.TOOL_CHANGED
        except Exception:
            return ToolErrorCategory.TOOL_CHANGED
        return None

    # ── execution ─────────────────────────────────────────────────────────────

    def _context(self, tool: Any, run_id: str) -> ToolCallContext:
        gate = _ProbedGate(
            state=PolicyState(mode=ExecutionMode.MANUAL),
            boundary=Boundary(self._workspace),
            ask=None,  # nobody can be asked: every ASK verdict is a refusal
            audit=self._audit,
        )
        granted = {
            ToolPermission(str(getattr(p, "value", p))) for p in tool.get_required_permissions()
        }
        return ToolCallContext(
            run_id=run_id,
            actor=_ACTOR,
            workspace=self._workspace,
            permissions=granted,
            hook_dispatcher=None,
            gate=gate,
        )

    async def _run(
        self, call: ValidatedToolCall, tool: Any, invocation_id: str, run_id: str
    ) -> ToolExecutionResult:
        ctx = self._context(tool, run_id)
        try:
            raw = await authorize_and_execute(tool, ctx, **call.arguments)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never echo the exception: its text may carry paths, values or secrets. Its type is
            # not evidence that the tool did not run; only a denial before the gate returned is.
            if _denied_before_entry(exc, ctx):
                return self._result(
                    call, invocation_id, ToolStatus.REFUSED, ToolErrorCategory.AUTHORIZATION_DENIED
                )
            return self._result(
                call,
                invocation_id,
                ToolStatus.FAILED,
                ToolErrorCategory.TOOL_ERROR,
                outcome=ToolOutcome.FAILED_DURING_EXECUTION,
                executed=True,
            )
        text, truncated, original = bound_output(raw, self._scrub)
        return self._result(
            call,
            invocation_id,
            ToolStatus.OK,
            None,
            output=text,
            truncated=truncated,
            original=original,
        )

    # ── controlled execution (Phase 4D-3) ─────────────────────────────────────

    async def _run_controlled(
        self,
        call: ValidatedToolCall,
        tool: Any,
        invocation_id: str,
        control: RunControl,
        key: str,
        reservation: Any,
    ) -> ToolExecutionResult:
        """Await the call without ever cancelling it; stop waiting if cancellation is requested."""
        ctx = self._context(tool, control.run_id)
        task: asyncio.Future[Any] = asyncio.ensure_future(
            authorize_and_execute(tool, ctx, **call.arguments)
        )
        waiter = asyncio.ensure_future(control.token.wait())
        abandoned = False
        try:
            try:
                await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                waiter.cancel()  # our own helper; the tool task is never cancelled
            if not task.done():
                abandoned = True
                result = self._result(
                    call,
                    invocation_id,
                    ToolStatus.FAILED,
                    ToolErrorCategory.OUTCOME_UNKNOWN,
                    outcome=ToolOutcome.CANCEL_REQUESTED_DURING_EXECUTION,
                    executed=True,
                    cancel_requested=True,
                    request_id=key,
                )
                control.store.settle(key, InvocationState.UNKNOWN, result)
                return result
            return self._finish(call, task, invocation_id, control, key, reservation, ctx)
        except asyncio.CancelledError:
            # Cancelled from outside. The tool is left running; record that we cannot say how it
            # ended. A later duplicate request gets this record, not a second run.
            abandoned = not task.done()
            unknown = self._result(
                call,
                invocation_id,
                ToolStatus.FAILED,
                ToolErrorCategory.OUTCOME_UNKNOWN,
                outcome=ToolOutcome.OUTCOME_UNKNOWN,
                executed=True,
                cancel_requested=True,
                request_id=key,
            )
            control.store.settle(key, InvocationState.UNKNOWN, unknown)
            raise
        finally:
            if abandoned:
                control.track(task)
                task.add_done_callback(lambda _t: reservation.release())
            else:
                reservation.release()  # no-op if _finish already settled it

    def _finish(
        self,
        call: ValidatedToolCall,
        task: asyncio.Future[Any],
        invocation_id: str,
        control: RunControl,
        key: str,
        reservation: Any,
        ctx: ToolCallContext,
    ) -> ToolExecutionResult:
        cancel_requested = control.token.cancelled
        if task.cancelled():
            result = self._result(
                call,
                invocation_id,
                ToolStatus.FAILED,
                ToolErrorCategory.OUTCOME_UNKNOWN,
                outcome=ToolOutcome.OUTCOME_UNKNOWN,
                executed=True,
                cancel_requested=cancel_requested,
                request_id=key,
            )
            control.store.settle(key, InvocationState.UNKNOWN, result)
            return result
        exc = task.exception()
        if exc is None:
            text, truncated, original = bound_output(task.result(), self._scrub)
            text, clipped = _clip_bytes(text, control.budget.result_allowance())
            reservation.settle(len(text.encode("utf-8")))
            late = control.budget.past_deadline()
            result = self._result(
                call,
                invocation_id,
                ToolStatus.FAILED if late else ToolStatus.OK,
                ToolErrorCategory.DEADLINE_EXCEEDED if late else None,
                outcome=ToolOutcome.DEADLINE_EXCEEDED if late else ToolOutcome.COMPLETED,
                executed=True,
                cancel_requested=cancel_requested,
                request_id=key,
                output=text,
                truncated=truncated or clipped,
                original=original,
            )
            control.store.settle(key, InvocationState.COMPLETED, result)
            return result
        if _denied_before_entry(exc, ctx):
            # Provable: the denial was raised before the gate's check returned, so before
            # ``guarded_execute`` and the tool body. The attempt is still counted and the approval is
            # not restored; the claim is released so trusted code may decide to try again.
            control.store.release(key)
            return self._result(
                call,
                invocation_id,
                ToolStatus.REFUSED,
                ToolErrorCategory.AUTHORIZATION_DENIED,
                request_id=key,
            )
        # Anything else, whatever its type (even a permission error or ToolBlockedError), may have
        # come from the tool body after it changed things. Recorded, never re-run, never retryable.
        result = self._result(
            call,
            invocation_id,
            ToolStatus.FAILED,
            ToolErrorCategory.TOOL_ERROR,
            outcome=ToolOutcome.FAILED_DURING_EXECUTION,
            executed=True,
            cancel_requested=cancel_requested,
            request_id=key,
        )
        control.store.settle(key, InvocationState.FAILED, result)
        return result

    def _duplicate(self, call: ValidatedToolCall, claim: Claim, key: str) -> ToolExecutionResult:
        if claim.kind is ClaimKind.CONFLICT:
            return self._result(
                call, "", ToolStatus.REFUSED, ToolErrorCategory.IDEMPOTENCY_CONFLICT, request_id=key
            )
        if claim.kind is ClaimKind.PENDING:
            return self._result(
                call, "", ToolStatus.REFUSED, ToolErrorCategory.DUPLICATE_PENDING, request_id=key
            )
        if isinstance(claim.result, ToolExecutionResult):
            return claim.result.model_copy(update={"replayed": True, "safe_to_retry": False})
        return self._result(  # a settled entry without a record: say we do not know
            call,
            "",
            ToolStatus.FAILED,
            ToolErrorCategory.OUTCOME_UNKNOWN,
            outcome=ToolOutcome.OUTCOME_UNKNOWN,
            executed=True,
            replayed=True,
            request_id=key,
        )

    def _cancelled_before(
        self, call: ValidatedToolCall, invocation_id: str, control: RunControl, key: str
    ) -> ToolExecutionResult:
        result = self._result(
            call,
            invocation_id,
            ToolStatus.REFUSED,
            ToolErrorCategory.CANCELLED,
            outcome=ToolOutcome.CANCELLED_BEFORE_EXECUTION,
            cancel_requested=True,
            request_id=key,
        )
        control.store.settle(key, InvocationState.CANCELLED, result)
        return result

    @staticmethod
    def _result(
        call: ValidatedToolCall,
        invocation_id: str,
        status: ToolStatus,
        error: ToolErrorCategory | None,
        *,
        output: str = "",
        truncated: bool = False,
        original: int = 0,
        outcome: ToolOutcome | None = None,
        executed: bool = False,
        cancel_requested: bool = False,
        replayed: bool = False,
        request_id: str = "",
    ) -> ToolExecutionResult:
        if outcome is None:
            outcome = {
                ToolStatus.OK: ToolOutcome.COMPLETED,
                ToolStatus.REFUSED: ToolOutcome.FAILED_BEFORE_EXECUTION,
                ToolStatus.FAILED: ToolOutcome.FAILED_DURING_EXECUTION,
            }[status]
        safe = (
            outcome is ToolOutcome.FAILED_BEFORE_EXECUTION
            and not executed
            and error not in _NEVER_SAFE_TO_RETRY
        )
        return ToolExecutionResult(
            invocation_id=invocation_id,
            call_id=call.call_id,
            tool=call.name,
            call_digest=call.call_digest,
            status=status,
            error=error,
            output=output,
            truncated=truncated,
            original_chars=original,
            request_id=request_id,
            outcome=outcome,
            executed=executed,
            cancel_requested=cancel_requested,
            replayed=replayed,
            safe_to_retry=safe,
        )


__all__ = [
    "MAX_OUTPUT_CHARS",
    "ToolErrorCategory",
    "ToolExecutionResult",
    "ToolOutcome",
    "ToolStatus",
    "TrustedToolExecutor",
    "bound_output",
]
