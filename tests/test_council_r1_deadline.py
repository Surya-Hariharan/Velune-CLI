"""R1 keeps the perspectives that finished when the stage deadline passes.

The runner's own timeout cancels a whole stage and would take finished seats with it. R1 therefore
runs under a slightly earlier deadline of its own: seats that already delivered are kept, the rest are
``timeout`` absences with no payload, and the run reports degraded or failed by the usual quorum rule.
Cancellation is a different thing and is still a cancellation.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.council_core_fakes import FakeSeatInvoker
from tests.council_scripted import (
    PERSPECTIVE_SEATS,
    make_request,
    valid_frame_json,
    valid_perspective_json,
)
from tests.council_wire import exploration_runner, explore
from velune.council.domain import StageId
from velune.council.report import perspectives_of
from velune.council.request import CouncilSettings
from velune.council.results import OutcomeStatus, SeatStatus, StageStatus
from velune.council.runner import CouncilCancelled
from velune.council.seatflow import STAGE_DEADLINE, deadline_for
from velune.council.stages import EXPLORATION_PLAN
from velune.council.trace import TraceEventKind

P = StageId.PERSPECTIVES
# Profile order is analyst, skeptic, creative, fact_checker, practicalist.
LAST_TWO = ("fact_checker", "practicalist")
LAST_THREE = ("creative", "fact_checker", "practicalist")
WALL_S = 1.0  # R1's allowance equals the wall budget here, so its own deadline is 0.9s


def run(coro, timeout: float = 30):
    return asyncio.run(asyncio.wait_for(coro, timeout))


class Events:
    def __init__(self) -> None:
        self.items = []

    def emit(self, event) -> None:
        self.items.append(event)

    def of(self, kind):
        return [e for e in self.items if e.event is kind]


class TimedInvoker(FakeSeatInvoker):
    """Perspective seats listed in ``slow`` never finish; ``stubborn`` ones ignore a first cancel."""

    def __init__(self, *, slow=(), stubborn=(), raises=(), unwind_s=1.0) -> None:
        super().__init__()
        self.slow, self.stubborn, self.raises = set(slow), set(stubborn), set(raises)
        self.unwind_s = unwind_s
        self.started: list[str] = []
        self.script("moderator", StageId.FRAME, valid_frame_json())
        for seat in PERSPECTIVE_SEATS:
            self.script(seat, P, valid_perspective_json(seat))

    async def invoke(self, call):
        if call.stage is P:
            self.started.append(call.seat_id)
            if call.seat_id in self.raises:
                raise RuntimeError("boom")
            if call.seat_id in self.slow:
                await asyncio.sleep(30)
            if call.seat_id in self.stubborn:
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    await asyncio.sleep(self.unwind_s)  # a job that is slow to honour a cancel
        return await super().invoke(call)


def settings(concurrency: int = 1) -> CouncilSettings:
    return CouncilSettings(wall_budget_s=WALL_S, max_concurrency=concurrency)


def explore_with(invoker, concurrency: int = 1):
    events = Events()
    request = make_request(settings=settings(concurrency))
    return run(explore(invoker, request, sink=events)), events


def statuses(outcome) -> dict[str, SeatStatus]:
    return {r.seat_id: r.status for r in outcome.stage_results[1].seat_results}


# ── the deadline itself ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("allowance", "expected"),
    [(1.0, 0.9), (10.0, 9.0), (50.0, 45.0), (100.0, 95.0), (600.0, 595.0)],
)
def test_the_deadline_is_the_allowance_less_five_seconds_or_ten_percent(allowance, expected):
    assert deadline_for(allowance) == pytest.approx(expected)
    assert 0 < deadline_for(allowance) < allowance


# ── partial delivery ────────────────────────────────────────────────────────


@pytest.mark.parametrize("concurrency", [1, 5])
def test_perspectives_that_finished_survive_the_deadline(concurrency):
    invoker = TimedInvoker(slow=LAST_TWO)
    outcome, events = explore_with(invoker, concurrency)
    frame_stage, r1 = outcome.stage_results
    assert list(perspectives_of(outcome)) == ["analyst", "skeptic", "creative"]
    assert statuses(outcome) == {
        "analyst": SeatStatus.OK,
        "skeptic": SeatStatus.OK,
        "creative": SeatStatus.OK,
        "fact_checker": SeatStatus.TIMEOUT,
        "practicalist": SeatStatus.TIMEOUT,
    }
    for result in r1.seat_results:
        if result.status is SeatStatus.TIMEOUT:
            assert result.payload is None and result.error.kind is SeatStatus.TIMEOUT
    assert [a.author for a in r1.artifacts] == ["analyst", "skeptic", "creative"]
    assert [r.seat_id for r in r1.seat_results] == list(PERSPECTIVE_SEATS)  # profile order
    assert STAGE_DEADLINE in r1.degradations
    assert "seat_unavailable:fact_checker" in r1.degradations
    assert r1.status is StageStatus.DEGRADED  # three ok, including the skeptic: quorum holds
    assert outcome.status is OutcomeStatus.DEGRADED
    assert outcome.answer is None and outcome.failure_summary is None
    assert not events.of(TraceEventKind.STAGE_TIMEOUT)  # the runner's timeout never had to fire


def test_the_surviving_perspectives_are_the_real_ones_not_stand_ins():
    outcome, _ = explore_with(TimedInvoker(slow=LAST_TWO), 5)
    for seat, perspective in perspectives_of(outcome).items():
        assert perspective.seat_id == seat
        assert perspective.position.startswith(f"SENT-{seat.upper()}")


def test_every_seat_result_is_traced_including_the_absences():
    outcome, events = explore_with(TimedInvoker(slow=LAST_TWO), 5)
    traced = {e.seat: e.status for e in events.of(TraceEventKind.SEAT_RESULT) if e.stage is P}
    assert traced["analyst"] == "ok" and traced["practicalist"] == "timeout"
    assert len(traced) == 5


def test_a_missing_quorum_fails_the_stage_but_keeps_the_delivered_perspectives():
    invoker = TimedInvoker(slow=LAST_THREE)
    outcome, _ = explore_with(invoker)
    r1 = outcome.stage_results[1]
    assert list(perspectives_of(outcome)) == ["analyst", "skeptic"]  # still retrievable
    assert r1.status is StageStatus.FAILED
    assert "quorum_not_met" in r1.degradations and STAGE_DEADLINE in r1.degradations
    assert outcome.status is OutcomeStatus.FAILED
    assert outcome.answer is None and outcome.failure_summary
    assert statuses(outcome)["creative"] is SeatStatus.TIMEOUT


def test_seats_that_never_started_are_timeouts_not_defaults():
    invoker = TimedInvoker(slow=("creative",))  # one at a time: creative hangs, the rest never run
    outcome, _ = explore_with(invoker, 1)
    assert invoker.started == ["analyst", "skeptic", "creative"]
    assert statuses(outcome)["fact_checker"] is SeatStatus.TIMEOUT
    assert statuses(outcome)["practicalist"] is SeatStatus.TIMEOUT
    assert list(perspectives_of(outcome)) == ["analyst", "skeptic"]


# ── an error before the deadline is still that error ────────────────────────


def test_a_seat_that_failed_before_the_deadline_stays_a_provider_error():
    invoker = TimedInvoker(slow=("fact_checker",), raises=("practicalist",))
    outcome, events = explore_with(invoker, 5)
    result = {r.seat_id: r for r in outcome.stage_results[1].seat_results}
    assert result["practicalist"].status is SeatStatus.PROVIDER_ERROR
    assert result["practicalist"].error.message == "internal_error:RuntimeError"
    assert result["fact_checker"].status is SeatStatus.TIMEOUT
    (error,) = events.of(TraceEventKind.SEAT_ERROR)
    assert error.seat == "practicalist" and error.detail == "RuntimeError"
    assert outcome.status is OutcomeStatus.DEGRADED and outcome.answer is None


def test_an_error_with_no_deadline_pressure_does_not_mention_the_deadline():
    invoker = TimedInvoker(raises=("practicalist",))
    outcome, _ = explore_with(invoker, 5)
    r1 = outcome.stage_results[1]
    assert STAGE_DEADLINE not in r1.degradations
    assert statuses(outcome)["practicalist"] is SeatStatus.PROVIDER_ERROR
    assert outcome.status is OutcomeStatus.DEGRADED


# ── cancellation is not a timeout ───────────────────────────────────────────


def test_cancellation_mid_r1_is_still_a_cancellation_and_leaves_nothing_running():
    invoker = TimedInvoker(slow=("fact_checker",))
    runner = exploration_runner(invoker)

    async def main():
        task = asyncio.ensure_future(runner.run(make_request(), plan=EXPLORATION_PLAN))
        for _ in range(400):
            if "fact_checker" in invoker.started:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(CouncilCancelled) as info:
            await task
        leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        return info.value.outcome, leftovers

    outcome, leftovers = asyncio.run(asyncio.wait_for(main(), 30))
    assert outcome.status is OutcomeStatus.CANCELLED and outcome.answer is None
    assert outcome.failure_summary == "cancelled during perspectives"
    assert [s.stage for s in outcome.stage_results] == [StageId.FRAME]  # the frame was kept
    assert all(STAGE_DEADLINE not in s.degradations for s in outcome.stage_results)
    assert leftovers == []


# ── the runner stays the backstop ───────────────────────────────────────────


def test_a_job_that_will_not_stop_still_ends_in_the_runners_stage_timeout():
    invoker = TimedInvoker(stubborn=("practicalist",), unwind_s=1.5)
    outcome, events = explore_with(invoker, 5)
    r1 = outcome.stage_results[1]
    assert r1.status is StageStatus.FAILED and "stage_timeout" in r1.degradations
    assert events.of(TraceEventKind.STAGE_TIMEOUT)
    assert outcome.status is OutcomeStatus.FAILED and outcome.answer is None
    assert perspectives_of(outcome) == {}  # nothing is claimed that the stage did not hand back
