"""Per-run execution control for the trusted tool executor: budgets, idempotency, cancellation.

Pure bookkeeping: this module imports no tool, permission or provider code and runs nothing. One
:class:`RunControl` belongs to exactly one run; there is no global mutable state, so two runs never
share a counter, a claim or a cancellation flag.

What can and cannot be enforced
-------------------------------
``authorize_and_execute`` is a coroutine, but a tool may still block the event loop or hand work to a
thread. Therefore:

* Counters, byte limits and the concurrency limit are enforced *before* a call starts and accounted
  when it settles. They are exact.
* The wall-clock limit is a **cooperative deadline**: checked before a call starts and again after it
  returns. Nothing here interrupts a running tool, so a call that overruns is only *reported* as late,
  and the underlying operation may have kept running until it returned.
* Cancellation is a flag. It is observed before a call starts and while the executor awaits the call.
  Once a call has started it is **never cancelled, interrupted or killed**: the executor stops waiting,
  reports that the operation may still be running or may have completed, and keeps the task so a
  caller can :meth:`RunControl.drain` it.

Idempotency here is "at most one execution attempt per (run, request id) that has begun", not
exactly-once side effects. A call that may have run is never run again by this layer.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import Field, model_validator

from velune._compat import StrEnum
from velune.council.serialization import Contract

MAX_TOOL_CALLS_LIMIT = 10_000
MAX_BYTES_LIMIT = 256 * 1024 * 1024
MAX_WALL_CLOCK_S = 86_400.0
MAX_CONCURRENCY_LIMIT = 8


class ExecutionBudget(Contract):
    """Immutable limits for one run. Every limit is required to be finite and positive.

    ``max_concurrent`` defaults to 1: calls are serialised unless a caller explicitly raises it.
    """

    max_tool_calls: int = Field(default=32, strict=True, gt=0, le=MAX_TOOL_CALLS_LIMIT)
    max_argument_bytes: int = Field(default=262_144, strict=True, gt=0, le=MAX_BYTES_LIMIT)
    max_result_bytes: int = Field(default=1_048_576, strict=True, gt=0, le=MAX_BYTES_LIMIT)
    max_single_result_bytes: int = Field(default=65_536, strict=True, gt=0, le=MAX_BYTES_LIMIT)
    wall_clock_s: float = Field(
        default=300.0, strict=True, gt=0, le=MAX_WALL_CLOCK_S, allow_inf_nan=False
    )
    max_concurrent: int = Field(default=1, strict=True, ge=1, le=MAX_CONCURRENCY_LIMIT)
    # Phase 4E-3. Counted in *calls*, not tokens. A model call is one ``propose`` (one runner turn);
    # a provider attempt is one request actually sent, so a retried call counts once as a model call
    # and once per try as an attempt.
    max_model_calls: int = Field(default=24, strict=True, gt=0, le=MAX_TOOL_CALLS_LIMIT)
    max_provider_attempts: int = Field(default=48, strict=True, gt=0, le=MAX_TOOL_CALLS_LIMIT)

    @model_validator(mode="after")
    def _single_fits_in_total(self) -> ExecutionBudget:
        if self.max_single_result_bytes > self.max_result_bytes:
            raise ValueError("max_single_result_bytes cannot exceed max_result_bytes")
        if not math.isfinite(self.wall_clock_s):
            raise ValueError("wall_clock_s must be finite")
        return self


class BudgetRefusal(StrEnum):
    """Why a call was not started. Nothing ran."""

    DEADLINE = "deadline"
    CALLS = "calls"
    ARGUMENT_BYTES = "argument_bytes"
    RESULT_BYTES = "result_bytes"
    CONCURRENCY = "concurrency"


class ModelCallRefusal(StrEnum):
    """Why a model call was not started. No provider request was made."""

    DEADLINE = "deadline"
    MODEL_CALLS = "model_calls"
    PROVIDER_ATTEMPTS = "provider_attempts"


class BudgetUsage(Contract):
    """A snapshot. The counters only ever increase."""

    calls: int
    argument_bytes: int
    result_bytes: int
    in_flight: int
    elapsed_s: float
    model_calls: int = 0
    provider_attempts: int = 0


class Reservation:
    """One started call's claim on the budget. Settle it once; further settles are no-ops."""

    def __init__(self, ledger: BudgetLedger) -> None:
        self._ledger = ledger
        self._settled = False

    def settle(self, result_bytes: int = 0) -> None:
        """Account the sanitized result size and free the concurrency slot."""
        if self._settled:
            return
        self._settled = True
        self._ledger._settle(result_bytes)

    def release(self) -> None:
        """Free the concurrency slot without accounting a result (output discarded or unknown)."""
        self.settle(0)


class BudgetLedger:
    """Mutable accounting for one run. All state changes happen under one lock with no ``await``."""

    def __init__(
        self, budget: ExecutionBudget, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.budget = budget
        self._clock = clock
        self._started = clock()
        self._deadline = self._started + budget.wall_clock_s
        self._lock = threading.Lock()
        self._calls = 0
        self._argument_bytes = 0
        self._result_bytes = 0
        self._in_flight = 0
        self._model_calls = 0
        self._provider_attempts = 0

    def past_deadline(self) -> bool:
        return self._clock() >= self._deadline

    def remaining_s(self) -> float:
        """Seconds left on the run's wall clock (0.0 once it has passed). Read-only."""
        return max(0.0, self._deadline - self._clock())

    def _problem(self, argument_bytes: int) -> BudgetRefusal | None:
        b = self.budget
        if self._clock() >= self._deadline:
            return BudgetRefusal.DEADLINE
        if self._calls + 1 > b.max_tool_calls:
            return BudgetRefusal.CALLS
        if self._argument_bytes + argument_bytes > b.max_argument_bytes:
            return BudgetRefusal.ARGUMENT_BYTES
        if self._result_bytes >= b.max_result_bytes:
            return BudgetRefusal.RESULT_BYTES
        if self._in_flight >= b.max_concurrent:
            return BudgetRefusal.CONCURRENCY
        return None

    def check(self, argument_bytes: int) -> BudgetRefusal | None:
        """Would a call start now? Changes nothing; ``reserve`` is the authority."""
        with self._lock:
            return self._problem(argument_bytes)

    def reserve(self, argument_bytes: int) -> Reservation | BudgetRefusal:
        """Atomically check and count one started call. Counted even if it later fails."""
        with self._lock:
            problem = self._problem(argument_bytes)
            if problem is not None:
                return problem
            self._calls += 1
            self._argument_bytes += argument_bytes
            self._in_flight += 1
            return Reservation(self)

    def reserve_model_call(self) -> ModelCallRefusal | None:
        """Atomically check and count one model call. ``None`` means it may be made.

        Checked before every provider request, so an exhausted budget or a passed deadline stops the
        run before any further request is sent. The deadline is the same cooperative wall clock the
        tool calls use: it is read here, it does not interrupt anything in flight.
        """
        with self._lock:
            if self._clock() >= self._deadline:
                return ModelCallRefusal.DEADLINE
            if self._model_calls + 1 > self.budget.max_model_calls:
                return ModelCallRefusal.MODEL_CALLS
            if self._provider_attempts >= self.budget.max_provider_attempts:
                return ModelCallRefusal.PROVIDER_ATTEMPTS
            self._model_calls += 1
            return None

    def reserve_provider_attempt(self) -> bool:
        """Atomically reserve ONE provider attempt, immediately before the request is sent.

        ``provider_attempts`` therefore counts *reserved / started* attempts: a reservation is never
        refunded, even if the request later fails, times out or is cancelled (the request may have
        reached the provider). ``False`` means the budget is spent and the request must not be sent.
        Check and increment happen under one lock with no ``await``, so concurrent callers cannot
        oversubscribe ``max_provider_attempts``. Model calls are counted separately.
        """
        with self._lock:
            if self._provider_attempts >= self.budget.max_provider_attempts:
                return False
            self._provider_attempts += 1
            return True

    def note_provider_attempts(self, attempts: int) -> None:
        """Account requests of an invoker that does not reserve per attempt (at least one).

        Legacy post-hoc path, used only when an invoker never touched its ``AttemptGate``. It cannot
        prevent overshoot; invokers that send provider requests should reserve instead.
        """
        with self._lock:
            self._provider_attempts += max(1, int(attempts))

    def result_allowance(self) -> int:
        """Bytes the next result may carry: the single-result cap, bounded by what is left."""
        with self._lock:
            left = self.budget.max_result_bytes - self._result_bytes
            return max(0, min(self.budget.max_single_result_bytes, left))

    def _settle(self, result_bytes: int) -> None:
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            self._result_bytes += max(0, result_bytes)

    def usage(self) -> BudgetUsage:
        with self._lock:
            return BudgetUsage(
                calls=self._calls,
                argument_bytes=self._argument_bytes,
                result_bytes=self._result_bytes,
                in_flight=self._in_flight,
                elapsed_s=max(0.0, self._clock() - self._started),
                model_calls=self._model_calls,
                provider_attempts=self._provider_attempts,
            )


class CancellationToken:
    """A one-way flag for one run. Setting it never interrupts anything that already started."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        """Request cancellation. Call from the event-loop thread. Idempotent."""
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()


class InvocationState(StrEnum):
    PENDING = "pending"  # claimed; may or may not have started
    COMPLETED = "completed"  # the tool returned
    FAILED = "failed"  # the tool was entered and raised
    CANCELLED = "cancelled"  # cancelled before it started: it never ran
    UNKNOWN = "unknown"  # may still be running, may have completed, may have changed things


class ClaimKind(StrEnum):
    NEW = "new"
    PENDING = "pending"  # a duplicate of a call still in progress
    SETTLED = "settled"  # a duplicate of a call that already has a recorded outcome
    CONFLICT = "conflict"  # same request id, different tool or arguments


@dataclass
class _Entry:
    tool: str
    call_digest: str
    state: InvocationState = InvocationState.PENDING
    result: Any = None


@dataclass(frozen=True)
class Claim:
    kind: ClaimKind
    state: InvocationState | None = None
    result: Any = None


class IdempotencyStore:
    """One run's record of which request ids have been claimed, and what became of them.

    The request id comes from trusted engine code (never from model output). An entry binds it to the
    tool name and ``call_digest``. Only outcomes where the tool may have run are kept as terminal;
    a call refused before it started is :meth:`release`-d so it can be attempted again.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._entries: dict[str, _Entry] = {}

    def claim(self, request_id: str, tool: str, call_digest: str) -> Claim:
        entry = self._entries.get(request_id)
        if entry is None:
            self._entries[request_id] = _Entry(tool, call_digest)
            return Claim(ClaimKind.NEW)
        if entry.tool != tool or entry.call_digest != call_digest:
            return Claim(ClaimKind.CONFLICT)
        if entry.state is InvocationState.PENDING:
            return Claim(ClaimKind.PENDING, entry.state)
        return Claim(ClaimKind.SETTLED, entry.state, entry.result)

    def settle(self, request_id: str, state: InvocationState, result: Any = None) -> None:
        entry = self._entries.get(request_id)
        if entry is None or state is InvocationState.PENDING:
            return
        entry.state, entry.result = state, result

    def release(self, request_id: str) -> None:
        """Forget a claim that never started. Settled entries are never released."""
        entry = self._entries.get(request_id)
        if entry is not None and entry.state is InvocationState.PENDING:
            del self._entries[request_id]

    def state_of(self, request_id: str) -> InvocationState | None:
        entry = self._entries.get(request_id)
        return entry.state if entry else None


@dataclass
class RunControl:
    """Everything the executor needs to bound and de-duplicate one run's tool calls."""

    run_id: str
    budget: BudgetLedger
    token: CancellationToken = field(default_factory=CancellationToken)
    store: IdempotencyStore = field(init=False)
    _background: set[asyncio.Future[Any]] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        self.store = IdempotencyStore(self.run_id)

    @classmethod
    def create(
        cls,
        run_id: str,
        budget: ExecutionBudget | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> RunControl:
        return cls(run_id, BudgetLedger(budget or ExecutionBudget(), clock))

    def track(self, task: asyncio.Future[Any]) -> None:
        """Keep a started call that was abandoned (not cancelled) and swallow its late outcome."""
        self._background.add(task)

        def _done(finished: asyncio.Future[Any]) -> None:
            self._background.discard(finished)
            if not finished.cancelled():
                finished.exception()  # mark retrieved; the output is discarded, never returned

        task.add_done_callback(_done)

    @property
    def abandoned_count(self) -> int:
        return len(self._background)

    async def drain(self) -> None:
        """Wait for calls the executor stopped waiting on. Never cancels them."""
        pending = list(self._background)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
