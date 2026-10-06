"""R1: five structurally typed, role-diverse perspectives, produced independently."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import pytest

from tests.council_core_fakes import FakeSeatInvoker
from tests.council_scripted import (
    PERSPECTIVE_SEATS,
    make_request,
    sentinel,
    valid_frame_json,
    valid_perspective_json,
)
from tests.council_wire import explore
from velune.cognition.execution_trace import CallReason
from velune.council.contracts import Perspective
from velune.council.domain import SeatKind, StageId
from velune.council.report import frame_of, perspectives_of
from velune.council.request import CouncilSettings, EvidenceItem
from velune.council.results import OutcomeStatus, StageStatus
from velune.council.serialization import canonical_json
from velune.council.trace import TraceEventKind

P = StageId.PERSPECTIVES


def fake_invoker(tag: str = "", **overrides) -> FakeSeatInvoker:
    invoker = FakeSeatInvoker()
    invoker.script("moderator", StageId.FRAME, valid_frame_json())
    for seat in PERSPECTIVE_SEATS:
        invoker.script(seat, P, *overrides.pop(seat, [valid_perspective_json(seat, tag)]))
    return invoker


def run(invoker, request=None, **kw):
    return asyncio.run(asyncio.wait_for(explore(invoker, request, **kw), timeout=20))


def test_healthy_run_yields_five_typed_perspectives_and_no_answer():
    outcome = run(fake_invoker())
    got = perspectives_of(outcome)
    assert list(got) == list(PERSPECTIVE_SEATS)  # profile order
    assert all(isinstance(p, Perspective) for p in got.values())
    assert outcome.status is OutcomeStatus.COMPLETED and outcome.answer is None
    assert [r.stage for r in outcome.stage_results] == [StageId.FRAME, P]
    assert all(r.status is StageStatus.COMPLETED for r in outcome.stage_results)
    assert frame_of(outcome, make_request()).degraded is False


def test_ids_are_assigned_by_the_core_per_seat():
    got = perspectives_of(run(fake_invoker()))
    prefixes = {
        "analyst": "AN",
        "skeptic": "SK",
        "creative": "CR",
        "fact_checker": "FC",
        "practicalist": "PR",
    }
    for seat, perspective in got.items():
        assert perspective.seat_id == seat
        assert [c.id for c in perspective.claims] == [f"{prefixes[seat]}-{i}" for i in (1, 2, 3)]
        assert perspective.claims[1].depends_on == (f"{prefixes[seat]}-1",)


def test_claim_ids_never_collide_across_seats():
    got = perspectives_of(run(fake_invoker()))
    ids = [c.id for p in got.values() for c in p.claims]
    assert len(ids) == len(set(ids)) == 15


def test_each_seat_is_called_once_with_its_own_role_and_reason():
    invoker = fake_invoker()
    run(invoker)
    r1 = [c for c in invoker.calls if c.stage is P]
    assert [c.seat_id for c in r1] == list(PERSPECTIVE_SEATS)
    assert all(c.kind is SeatKind.PERSPECTIVE and c.reason is CallReason.PRIMARY for c in r1)
    systems = {c.seat_id: c.messages[0].content for c in r1}
    assert len(set(systems.values())) == 5  # five different roles, not five copies of one
    for seat, system in systems.items():
        assert f'<seat id="{seat}">' in system and "Your role:" in system


def test_role_titles_appear_only_in_the_seats_own_role_text():
    invoker = fake_invoker()
    run(invoker)
    titles = {
        "analyst": "Your role: ANALYST",
        "skeptic": "Your role: SKEPTIC",
        "creative": "Your role: CREATIVE",
        "fact_checker": "Your role: FACT CHECKER",
        "practicalist": "Your role: PRACTICALIST",
    }
    for call in (c for c in invoker.calls if c.stage is P):
        system = call.messages[0].content
        assert titles[call.seat_id] in system
        for seat, title in titles.items():
            if seat != call.seat_id:
                assert title not in system


def test_evidence_is_identical_for_every_seat():
    request = make_request(evidence=(EvidenceItem(id="e1", text="a shared fact", source="doc"),))
    invoker = fake_invoker()
    run(invoker, request)
    users = {c.messages[1].content.split("<frame>")[0] for c in invoker.calls if c.stage is P}
    assert (
        len(users) == 1 and '<evidence id="e1" source="doc">a shared fact</evidence>' in users.pop()
    )


def test_the_model_cannot_claim_another_seats_identity():
    forged = valid_perspective_json("analyst", seat_id="skeptic")
    invoker = fake_invoker(analyst=[forged, valid_perspective_json("analyst")])
    outcome = run(invoker)
    assert perspectives_of(outcome)["analyst"].seat_id == "analyst"
    assert len(invoker.calls_for("analyst", P)) == 2  # the forgery cost one repair


def test_model_chosen_claim_ids_are_rejected():
    body = json.loads(valid_perspective_json("analyst"))
    body["claims"][0]["id"] = "SK-1"
    invoker = fake_invoker(analyst=[json.dumps(body), valid_perspective_json("analyst")])
    perspective = perspectives_of(run(invoker))["analyst"]
    assert perspective.claims[0].id == "AN-1"


@pytest.mark.parametrize("hidden", ["reasoning", "thoughts", "scratchpad", "chain_of_thought"])
def test_private_reasoning_never_reaches_a_perspective(hidden):
    bad = valid_perspective_json("creative", **{hidden: "HIDDEN-NOTES"})
    invoker = fake_invoker(creative=[bad, valid_perspective_json("creative")])
    outcome = run(invoker)
    assert "HIDDEN-NOTES" not in canonical_json(outcome)
    repair = invoker.calls_for("creative", P)[1].messages
    assert repair[2].role == "assistant" and "HIDDEN-NOTES" in repair[2].content  # its own reply
    assert "HIDDEN-NOTES" not in repair[3].content  # the error note names the field, not the text
    assert hidden in repair[3].content


def test_too_many_claims_or_bad_dependencies_trigger_repair_not_truncation():
    claim = {"text": "c", "status": "known", "confidence": 0.5}
    nine = valid_perspective_json("analyst", claims=[claim] * 9)
    body = json.loads(valid_perspective_json("skeptic"))
    body["claims"][0]["depends_on"] = [2]
    invoker = fake_invoker(
        analyst=[nine, valid_perspective_json("analyst")],
        skeptic=[json.dumps(body), valid_perspective_json("skeptic")],
    )
    outcome = run(invoker)
    assert len(perspectives_of(outcome)["analyst"].claims) == 3
    assert len(invoker.calls_for("analyst", P)) == 2 and len(invoker.calls_for("skeptic", P)) == 2
    assert "depends_on" in invoker.calls_for("skeptic", P)[1].messages[3].content


def test_fenced_replies_are_accepted_and_trailing_prose_is_repaired():
    fenced = "```json\n" + valid_perspective_json("analyst") + "\n```"
    chatty = valid_perspective_json("skeptic") + "\nLet me know if you need more."
    invoker = fake_invoker(analyst=[fenced], skeptic=[chatty, valid_perspective_json("skeptic")])
    outcome = run(invoker)
    assert set(perspectives_of(outcome)) == set(PERSPECTIVE_SEATS)
    assert len(invoker.calls_for("analyst", P)) == 1 and len(invoker.calls_for("skeptic", P)) == 2


def test_seats_run_in_parallel_only_when_configured():
    for limit, expected in ((1, 1), (5, 5)):
        invoker = fake_invoker()
        invoker.delay_s = 0.02
        run(invoker, make_request(settings=CouncilSettings(max_concurrency=limit)))
        assert invoker.max_in_flight == expected


def test_trace_events_are_flushed_in_profile_order_whatever_the_concurrency():
    class Sink:
        def __init__(self):
            self.items = []

        def emit(self, event):
            self.items.append(event)

    digests = set()
    for limit in (1, 5):
        sink = Sink()
        invoker = fake_invoker(
            skeptic=["bad", valid_perspective_json("skeptic")],
            creative=["bad too", valid_perspective_json("creative")],
        )
        invoker.delay_s = 0.005
        outcome = run(
            invoker, make_request(settings=CouncilSettings(max_concurrency=limit)), sink=sink
        )
        repairs = [e.seat for e in sink.items if e.event is TraceEventKind.SEAT_REPAIR]
        assert repairs == ["skeptic", "creative"]
        digests.add(outcome.trace_digest)
    assert len(digests) == 1


def test_the_same_run_twice_is_byte_identical():
    a = run(fake_invoker())
    b = run(fake_invoker())
    assert canonical_json(a) == canonical_json(b) and a.trace_digest == b.trace_digest


_SCRIPT = """
import asyncio
from tests.council_core_fakes import FakeSeatInvoker
from tests.council_scripted import PERSPECTIVE_SEATS, valid_frame_json, valid_perspective_json
from tests.council_wire import explore
from velune.council.domain import StageId
from velune.council.serialization import canonical_json, digest

invoker = FakeSeatInvoker()
invoker.script("moderator", StageId.FRAME, valid_frame_json())
for seat in PERSPECTIVE_SEATS:
    invoker.script(seat, StageId.PERSPECTIVES, valid_perspective_json(seat))
outcome = asyncio.run(explore(invoker))
print(digest(outcome), outcome.trace_digest)
"""


def test_outcome_digest_is_identical_across_hash_seeds():
    seen = set()
    for seed in ("0", "7", "random"):
        run_ = subprocess.run(
            [sys.executable, "-c", _SCRIPT],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
            cwd=os.getcwd(),
            timeout=120,
        )
        assert run_.returncode == 0, run_.stderr
        seen.add(run_.stdout.strip())
    assert len(seen) == 1


def test_perspectives_carry_no_seats_peer_text_by_construction():
    outcome = run(fake_invoker())
    for seat, perspective in perspectives_of(outcome).items():
        text = canonical_json(perspective)
        assert sentinel(seat) in text
        for other in PERSPECTIVE_SEATS:
            if other != seat:
                assert sentinel(other) not in text
