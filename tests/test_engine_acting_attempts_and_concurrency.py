"""Phase 4E-3 remediation: atomic provider-attempt reservation and provider-wide concurrency.

Fake providers and fake sleeps only; no network, no live provider.
"""

from __future__ import annotations

import asyncio
from typing import Any

from tests.test_engine_acting_reliability import (
    CATALOG,
    Flaky,
    Sleeper,
    build,
    go,
    invoker,
)
from tests.test_engine_provider_acting import areq
from tests.test_engine_provider_integration import MODEL, ScriptedProvider, reply
from velune.core.errors.provider import InferenceError, ProviderAuthenticationError
from velune.orchestration.engine.acting import ActingFailureKind, _AttemptGate
from velune.orchestration.engine.execcontrol import BudgetLedger, ExecutionBudget
from velune.orchestration.engine_adapters.provider_acting import (
    ProviderActingInvoker,
    ProviderRetryPolicy,
    Rejection,
    fixed_failure_messages,
)
from velune.providers.retrying import RetryingProvider


def ledger(attempts: int) -> BudgetLedger:
    return BudgetLedger(ExecutionBudget(max_provider_attempts=attempts))


def gated(led: BudgetLedger, **kw: Any):
    return areq(attempt_gate=_AttemptGate(led), **kw)


class Probe:
    """Counts requests and the peak number in flight."""

    provider_id = "probe"

    def __init__(self, delay: float = 0.01) -> None:
        self.delay = delay
        self.requests = 0
        self.in_flight = 0
        self.peak = 0

    async def infer(self, request):
        self.requests += 1
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            return reply("ok", stop="stop")
        finally:
            self.in_flight -= 1


# ── Fix 1: atomic provider-attempt reservation ────────────────────────────────


def test_the_ledger_reserves_atomically_and_never_exceeds_the_maximum():
    led = ledger(3)
    assert [led.reserve_provider_attempt() for _ in range(5)] == [True, True, True, False, False]
    assert led.usage().provider_attempts == 3 and led.usage().model_calls == 0


async def test_a_denied_reservation_sends_nothing():
    led = ledger(1)
    assert led.reserve_provider_attempt()  # the budget is already spent
    provider = ScriptedProvider(reply("never", stop="stop"))
    result = await invoker(provider).propose(gated(led))
    assert not result.ok and provider.requests == []
    assert result.error.message in fixed_failure_messages()
    assert (
        result.error.message
        == "the provider-attempt budget is exhausted and the request was not sent"
    )
    assert result.attempts == 0 and led.usage().provider_attempts == 1


async def test_each_retry_takes_its_own_reservation_and_the_cap_stops_the_chain():
    led = ledger(2)
    provider = Flaky(*[InferenceError("500")] * 10)
    inv = invoker(provider, retry=ProviderRetryPolicy(max_attempts=5))
    result = await inv.propose(gated(led))
    assert not result.ok and len(provider.requests) == 2  # the third try was denied, not sent
    assert led.usage().provider_attempts == 2
    assert (
        result.error.message
        == "the provider-attempt budget is exhausted and the request was not sent"
    )
    assert result.attempts == 2


async def test_a_failed_request_is_not_refunded():
    led = ledger(5)
    provider = Flaky(ProviderAuthenticationError("bad key"))
    result = await invoker(provider).propose(gated(led))
    assert not result.ok and led.usage().provider_attempts == 1
    # a second call cannot buy the attempt back
    again = await invoker(Flaky(ProviderAuthenticationError("bad key"))).propose(gated(led))
    assert not again.ok and led.usage().provider_attempts == 2


async def test_concurrent_requests_cannot_oversubscribe_the_budget():
    led = ledger(5)
    provider = Probe()
    invokers = [invoker(provider) for _ in range(4)]
    results = await asyncio.gather(
        *(invokers[i % 4].propose(gated(led)) for i in range(40)), return_exceptions=True
    )
    ok = [r for r in results if not isinstance(r, BaseException) and r.ok]
    denied = [r for r in results if not isinstance(r, BaseException) and not r.ok]
    assert len(ok) == 5 and len(denied) == 35
    assert provider.requests == 5 and led.usage().provider_attempts == 5
    assert all(r.attempts == 0 for r in denied)


async def test_concurrent_retries_share_the_budget_without_overshoot():
    led = ledger(7)
    provider = Flaky(*[InferenceError("500")] * 100)
    invs = [invoker(provider, retry=ProviderRetryPolicy(max_attempts=5)) for _ in range(6)]
    await asyncio.gather(*(i.propose(gated(led)) for i in invs))
    assert len(provider.requests) == 7 and led.usage().provider_attempts == 7


async def test_cancellation_keeps_the_reservation_and_permits_no_extra_attempt():
    led = ledger(2)
    started = asyncio.Event()

    class Hang:
        provider_id = "hang"
        requests = 0

        async def infer(self, request):
            Hang.requests += 1
            started.set()
            await asyncio.sleep(60)

    inv = invoker(Hang())
    task = asyncio.ensure_future(inv.propose(gated(led)))
    await started.wait()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert Hang.requests == 1 and led.usage().provider_attempts == 1  # consumed, not refunded
    assert led.reserve_provider_attempt() and not led.reserve_provider_attempt()  # one left


async def test_a_deadline_while_queued_for_a_permit_consumes_no_attempt():
    led = ledger(5)
    release = asyncio.Event()

    class Holder:
        provider_id = "holder"

        async def infer(self, request):
            await release.wait()
            return reply("ok", stop="stop")

    wrapped = RetryingProvider(Holder(), max_concurrent=1)
    first = asyncio.ensure_future(invoker(wrapped).propose(areq()))
    await asyncio.sleep(0.01)  # it now holds the only permit
    queued = await invoker(wrapped).propose(gated(led, timeout_s=0.05))
    assert not queued.ok and queued.attempts == 0 and led.usage().provider_attempts == 0
    release.set()
    assert (await first).ok


async def test_the_runner_stops_retries_at_the_attempt_budget_with_the_stable_error(tmp_path):
    provider = Flaky(*[InferenceError("500")] * 10)
    runner, ctl, *_ = build(
        tmp_path,
        provider,
        budget=ExecutionBudget(max_provider_attempts=2),
        retry=ProviderRetryPolicy(max_attempts=5),
        sleep=Sleeper(),
    )
    outcome = await go(runner, ctl)
    assert outcome.failure.kind is ActingFailureKind.MODEL_BUDGET
    assert len(provider.requests) == 2 and ctl.budget.usage().provider_attempts == 2


async def test_an_invoker_that_does_not_reserve_is_still_accounted_after_the_fact():
    led = ledger(5)
    gate = _AttemptGate(led)
    assert gate.reserved == 0 and not gate.denied
    # (the runner's fallback path is covered by the existing FakeActingInvoker tests)
    assert gate.reserve() and gate.reserved == 1 and led.usage().provider_attempts == 1


# ── Fix 2: provider-wide concurrency across acting runs ───────────────────────


async def test_runs_sharing_a_wrapped_provider_obey_its_concurrency_limit():
    inner = Probe()
    wrapped = RetryingProvider(inner, max_concurrent=2)
    runs = [invoker(wrapped) for _ in range(3)]  # three independent acting runs
    results = await asyncio.gather(*(runs[i % 3].propose(areq()) for i in range(12)))
    assert all(r.ok for r in results)
    assert inner.peak == 2 and inner.requests == 12  # the limit holds across runs, and is reached


async def test_runs_sharing_an_unwrapped_provider_obey_the_shared_default_limit():
    provider = Probe()
    runs = [invoker(provider) for _ in range(3)]
    await asyncio.gather(*(runs[i % 3].propose(areq()) for i in range(30)))
    assert provider.peak == 4 and provider.requests == 30


async def test_acting_shares_the_wrapper_gate_with_other_users_of_the_wrapper():
    inner = Probe(delay=0.02)
    wrapped = RetryingProvider(inner, max_concurrent=2)
    from velune.core.types.inference import InferenceRequest

    chat = InferenceRequest(model_id=MODEL, messages=[{"role": "user", "content": "hi"}])
    await asyncio.gather(
        *(wrapped.infer(chat) for _ in range(4)),
        *(invoker(wrapped).propose(areq()) for _ in range(4)),
    )
    assert inner.peak == 2


async def test_the_permit_is_released_after_an_error_a_timeout_and_a_cancellation():
    wrapped = RetryingProvider(Flaky(ProviderAuthenticationError("x")), max_concurrent=1)
    gate = wrapped._concurrency_gate
    assert not (await invoker(wrapped).propose(areq())).ok  # error
    assert not gate.locked()

    class Slow:
        provider_id = "slow"

        async def infer(self, request):
            await asyncio.sleep(60)

    slow = RetryingProvider(Slow(), max_concurrent=1)
    timed_out = await invoker(slow).propose(areq(timeout_s=0.05))  # timeout
    assert (
        timed_out.error.message == "the provider call timed out"
        and not slow._concurrency_gate.locked()
    )

    task = asyncio.ensure_future(invoker(slow).propose(areq()))
    await asyncio.sleep(0.02)
    assert slow._concurrency_gate.locked()
    task.cancel()  # cancellation
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert not slow._concurrency_gate.locked()

    healthy = RetryingProvider(Probe(), max_concurrent=1)  # nothing leaked: it still works
    assert (await invoker(healthy).propose(areq())).ok


async def test_the_permit_is_not_held_while_waiting_to_retry():
    provider = Flaky(InferenceError("500"))
    wrapped = RetryingProvider(provider, max_concurrent=1)
    held_during_delay: list[bool] = []

    async def sleep(delay: float) -> None:
        held_during_delay.append(wrapped._concurrency_gate.locked())

    result = await invoker(wrapped, sleep=sleep).propose(areq())
    assert result.ok and result.attempts == 2 and held_during_delay == [False]


async def test_retries_are_owned_by_the_acting_adapter_not_duplicated_by_the_wrapper():
    inner = Flaky(*[InferenceError("500")] * 20)
    wrapped = RetryingProvider(inner, max_attempts=3)  # would be 3 x 3 if it were left in the path
    result = await invoker(wrapped, retry=ProviderRetryPolicy(max_attempts=2)).propose(areq())
    assert not result.ok and result.attempts == 2 and len(inner.requests) == 2
    assert Rejection.PROVIDER_ERROR.value == "provider_error"


async def test_no_gate_is_leaked_for_a_provider_that_cannot_be_weakly_referenced():
    class Slotted:
        __slots__ = ()
        provider_id = "slotted"

        async def infer(self, request):
            return reply("ok", stop="stop")

    inv = ProviderActingInvoker(Slotted(), MODEL, CATALOG)
    assert (await inv.propose(areq())).ok
