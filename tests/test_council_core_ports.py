"""The port contract, the fake that honours it, the reference scheduler and the trace log."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from tests.council_core_fakes import (
    CounterIds,
    FakeAssignments,
    FakeClock,
    FakeSeatInvoker,
)
from velune.cognition.execution_trace import CallReason
from velune.council.domain import SeatKind, StageId
from velune.council.ports import (
    AssignmentSource,
    AsyncioScheduler,
    Clock,
    IdSource,
    SeatCall,
    SeatInvoker,
    SeatMessage,
    TraceSink,
)
from velune.council.results import SeatStatus
from velune.council.trace import CouncilTraceEvent, TraceEventKind, TraceLog

P = StageId.PERSPECTIVES


def call(seat: str = "analyst", **kw) -> SeatCall:
    return SeatCall(
        seat_id=seat,
        kind=SeatKind.PERSPECTIVE,
        stage=P,
        messages=(SeatMessage(role="user", content="hi"),),
        **kw,
    )


def run(coro, timeout: float = 10):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def test_fakes_satisfy_the_port_protocols():
    assert isinstance(FakeSeatInvoker(), SeatInvoker)
    assert isinstance(FakeClock(), Clock)
    assert isinstance(CounterIds(), IdSource)
    assert isinstance(FakeAssignments(), AssignmentSource)
    assert isinstance(AsyncioScheduler(), object)

    class Sink:
        def emit(self, event: CouncilTraceEvent) -> None: ...

    assert isinstance(Sink(), TraceSink)


def test_a_seat_call_names_a_seat_never_a_provider():
    fields = set(SeatCall.model_fields)
    assert not fields & {"provider", "provider_id", "model", "model_id", "api_key"}
    assert call().reason is CallReason.PRIMARY
    with pytest.raises(ValidationError):
        call(provider="groq")
    with pytest.raises(ValidationError):
        SeatCall(seat_id="a", kind=SeatKind.PERSPECTIVE, stage=P, messages=())


def test_invoker_returns_typed_ok_and_typed_failures():
    invoker = FakeSeatInvoker()
    invoker.script("analyst", P, "hello")
    invoker.script("skeptic", P, SeatStatus.TIMEOUT)
    ok = run(invoker.invoke(call("analyst")))
    bad = run(invoker.invoke(call("skeptic")))
    assert ok.ok and ok.payload == "hello"
    assert not bad.ok and bad.payload is None and bad.status is SeatStatus.TIMEOUT


def test_invoker_is_deterministic_and_records_calls():
    invoker = FakeSeatInvoker()
    first = run(invoker.invoke(call("analyst")))
    second = run(invoker.invoke(call("analyst")))
    assert first == second
    assert len(invoker.calls_for("analyst")) == 2


def test_invoker_script_consumes_in_order_then_repeats_last():
    invoker = FakeSeatInvoker()
    invoker.script("analyst", P, SeatStatus.TIMEOUT, "second")
    statuses = [run(invoker.invoke(call("analyst"))).status for _ in range(3)]
    assert statuses == [SeatStatus.TIMEOUT, SeatStatus.OK, SeatStatus.OK]


def test_scheduler_preserves_order_and_isolates_failures():
    async def scenario():
        async def slow():
            await asyncio.sleep(0.02)
            return "slow"

        async def fast():
            return "fast"

        async def boom():
            raise RuntimeError("bad job")

        return await AsyncioScheduler().run([slow, boom, fast], max_concurrency=3)

    out = run(scenario())
    assert out[0] == "slow" and out[2] == "fast"
    assert isinstance(out[1], RuntimeError)


def test_scheduler_respects_max_concurrency():
    async def scenario(limit):
        live = peak = 0

        async def job():
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01)
            live -= 1

        await AsyncioScheduler().run([job] * 6, max_concurrency=limit)
        return peak

    assert run(scenario(1)) == 1
    assert run(scenario(3)) == 3


def test_scheduler_cancellation_cancels_pending_jobs_and_propagates():
    async def scenario():
        started, finished = [], []

        async def job(i):
            started.append(i)
            await asyncio.sleep(5)
            finished.append(i)

        task = asyncio.ensure_future(
            AsyncioScheduler().run([lambda i=i: job(i) for i in range(4)], max_concurrency=4)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        return started, finished, leftovers

    started, finished, leftovers = run(scenario())
    assert len(started) == 4 and finished == [] and leftovers == []


def test_trace_log_is_ordered_and_timestamp_free():
    log = TraceLog()
    log.emit(TraceEventKind.RUN_STARTED)
    log.emit(TraceEventKind.STAGE_STARTED, stage=P)
    log.emit(TraceEventKind.SEAT_RESULT, stage=P, seat="analyst", status="ok")
    assert [e.seq for e in log.events] == [0, 1, 2]
    assert not {"time", "timestamp", "at"} & set(CouncilTraceEvent.model_fields)


def test_trace_digest_is_reproducible_and_sensitive():
    def build(status: str):
        log = TraceLog()
        log.emit(TraceEventKind.SEAT_RESULT, stage=P, seat="analyst", status=status)
        return log.digest()

    assert build("ok") == build("ok")
    assert build("ok") != build("timeout")


def test_a_broken_sink_never_breaks_the_run():
    class Broken:
        def emit(self, event):
            raise RuntimeError("sink down")

    log = TraceLog(Broken())
    log.emit(TraceEventKind.RUN_STARTED)
    log.emit(TraceEventKind.RUN_FINISHED)
    assert len(log.events) == 2


def test_sink_receives_events_in_order():
    seen: list[int] = []

    class Sink:
        def emit(self, event):
            seen.append(event.seq)

    log = TraceLog(Sink())
    for _ in range(3):
        log.emit(TraceEventKind.STAGE_STARTED, stage=P)
    assert seen == [0, 1, 2]


def test_fake_clock_and_ids_are_exact():
    clock, ids = FakeClock(10.0), CounterIds()
    clock.advance(2.5)
    assert clock.monotonic() == 12.5
    assert [ids.next_id("run"), ids.next_id("run"), ids.next_id("x")] == ["run-1", "run-2", "x-1"]
