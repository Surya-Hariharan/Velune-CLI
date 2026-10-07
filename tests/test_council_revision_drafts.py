"""R3 wire drafts: a strict patch on the seat's own perspective, with core-owned bookkeeping."""

from __future__ import annotations

import json

import pytest

from tests.council_scripted import valid_perspective_json
from velune.council.contracts import (
    CauseRef,
    ChangeKind,
    Critique,
    Disagreement,
    DisagreementKind,
    RevisionStatus,
    Severity,
)
from velune.council.drafts import PerspectiveDraft, perspective_from_draft
from velune.council.parsing import DraftParseError, parse_and_convert
from velune.council.profiles import GENERAL_PROFILE
from velune.council.revisiondrafts import (
    RevisionDraft,
    objections_of,
    required_responses,
    revision_from_draft,
)
from velune.council.serialization import canonical_json

ANALYST = GENERAL_PROFILE.seat("analyst")
R1 = perspective_from_draft(
    PerspectiveDraft.model_validate_json(valid_perspective_json("analyst", "t")), seat=ANALYST
)


def critique(reviewer, items):
    return (
        reviewer,
        Critique(
            reviewer_seat=reviewer,
            target_seat="analyst",
            steelman="the best point",
            disagreements=tuple(
                Disagreement(
                    claim_id=claim_id,
                    objection=f"objection to {claim_id}",
                    kind=DisagreementKind.UNSUPPORTED,
                    severity=Severity(severity),
                )
                for claim_id, severity in items
            ),
            confidence=0.6,
        ),
    )


# skeptic #1 major (AN-1), skeptic #2 minor (AN-2), fact_checker #1 critical (AN-3)
CRITIQUES = [
    critique("skeptic", [("AN-1", "major"), ("AN-2", "minor")]),
    critique("fact_checker", [("AN-3", "critical")]),
]
ACCEPT_BOTH = [
    {"reviewer": "skeptic", "objection": 1, "decision": "accept", "note": "fair"},
    {"reviewer": "fact_checker", "objection": 1, "decision": "accept", "note": "fair"},
]
REJECT_BOTH = [{**r, "decision": "reject"} for r in ACCEPT_BOTH]


def body(responses=None, **rest):
    return {"responses": ACCEPT_BOTH if responses is None else responses, **rest}


def cause(reviewer="skeptic", objection=1):
    return {"reviewer": reviewer, "objection": objection}


def edit(op="modify", claim_id="AN-1", reason="because", caused_by=None, **fields):
    item = {"op": op, "reason": reason, "caused_by": caused_by or [cause()], **fields}
    if claim_id is not None:
        item["claim_id"] = claim_id
    return item


def add(**fields):
    return edit("add", None, **fields)


def convert(payload, critiques=CRITIQUES):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return parse_and_convert(
        text,
        RevisionDraft,
        lambda draft: revision_from_draft(
            draft, seat=ANALYST, perspective=R1, critiques=critiques, profile=GENERAL_PROFILE
        ),
    )


def rejects(payload, fragment="", critiques=CRITIQUES):
    with pytest.raises(DraftParseError) as info:
        convert(payload, critiques)
    assert fragment in " ".join(info.value.errors), info.value.errors


# ── objections ──────────────────────────────────────────────────────────────


def test_objections_are_numbered_per_reviewer_from_one_and_only_major_or_critical_are_required():
    assert list(objections_of(CRITIQUES)) == [("skeptic", 1), ("skeptic", 2), ("fact_checker", 1)]
    assert required_responses(CRITIQUES) == [("skeptic", 1), ("fact_checker", 1)]


# ── strictness ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "extra",
    [
        "seat_id",
        "status",
        "claims",
        "changes",
        "accepted",
        "rejected",
        "deferred",
        "confidence_delta",
        "revised_position",
        "schema_version",
        "reasoning",
        "thoughts",
        "scratchpad",
        "chain_of_thought",
    ],
)
def test_unknown_fields_are_rejected_at_every_level(extra):
    rejects({**body(), extra: "x"})
    base = body(edits=[edit(text="x")])
    base["edits"][0][extra] = "x"
    rejects(base)
    inner = body()
    inner["responses"] = [{**inner["responses"][0], extra: "x"}, inner["responses"][1]]
    rejects(inner)


def test_the_model_cannot_set_the_seat_the_status_or_the_delta():
    for field, value in (
        ("seat_id", "skeptic"),
        ("status", "reaffirmed"),
        ("confidence_delta", 0.3),
    ):
        rejects({**body(REJECT_BOTH), field: value})


def test_size_and_shape_limits_hold():
    rejects(body(edits=[edit(text="x")] * 9))
    rejects(body(position="x" * 401))
    rejects(body(remaining_uncertainties=["a"] * 7))
    rejects(body(responses=[{**ACCEPT_BOTH[0], "objection": 0}, ACCEPT_BOTH[1]]))
    rejects(body(responses=[{**ACCEPT_BOTH[0], "decision": "maybe"}, ACCEPT_BOTH[1]]))
    rejects(body(edits=[{**edit(text="x"), "caused_by": []}]))


# ── responses ───────────────────────────────────────────────────────────────


def test_every_major_or_critical_objection_needs_exactly_one_response():
    rejects(body(responses=[ACCEPT_BOTH[0]]), "needs a response")
    rejects(body(responses=[ACCEPT_BOTH[0], ACCEPT_BOTH[0]]), "answered twice")
    rejects(
        body(responses=[*ACCEPT_BOTH, {**ACCEPT_BOTH[0], "reviewer": "creative"}]),
        "there is no objection",
    )
    rejects(body(responses=[*ACCEPT_BOTH, {**ACCEPT_BOTH[0], "objection": 5}]), "no objection 5")


def test_a_minor_objection_may_be_answered_or_left():
    left = convert(body(REJECT_BOTH))
    answered = convert(
        body(
            [
                *REJECT_BOTH,
                {"reviewer": "skeptic", "objection": 2, "decision": "reject", "note": "n"},
            ]
        )
    )
    assert len(left.rejected) == 2 and len(answered.rejected) == 3


def test_the_decisions_land_in_the_right_lists_in_reading_order():
    responses = [
        {
            "reviewer": "fact_checker",
            "objection": 1,
            "decision": "insufficient_evidence",
            "note": "c",
        },
        {"reviewer": "skeptic", "objection": 1, "decision": "partial", "note": "a"},
        {"reviewer": "skeptic", "objection": 2, "decision": "reject", "note": "b"},
    ]
    revision = convert(body(responses))
    assert [c.critique for c in revision.accepted] == [
        CauseRef(reviewer_seat="skeptic", objection_index=0)
    ]
    assert [c.critique for c in revision.rejected] == [
        CauseRef(reviewer_seat="skeptic", objection_index=1)
    ]
    assert [c.critique for c in revision.deferred] == [
        CauseRef(reviewer_seat="fact_checker", objection_index=0)
    ]


# ── reaffirming ─────────────────────────────────────────────────────────────


def test_rejecting_everything_with_no_edits_reaffirms_the_original_exactly():
    revision = convert(body(REJECT_BOTH))
    assert revision.status is RevisionStatus.REAFFIRMED
    assert revision.claims == R1.claims and revision.revised_position == R1.position
    assert revision.revised_confidence == R1.confidence and revision.confidence_delta == 0.0
    assert revision.changes == () and revision.accepted == () and revision.seat_id == "analyst"


def test_accepting_without_changing_anything_is_still_honest_and_revised_only_if_something_moved():
    assert convert(body()).status is RevisionStatus.REAFFIRMED  # accepted, nothing to change
    assert convert(body(confidence=0.4)).status is RevisionStatus.REVISED


# ── edits ───────────────────────────────────────────────────────────────────


def test_a_modification_changes_only_that_claim_and_keeps_its_id():
    revision = convert(body(edits=[edit(text="a better first claim")]))
    assert revision.status is RevisionStatus.REVISED
    assert [c.id for c in revision.claims] == ["AN-1", "AN-2", "AN-3"]
    assert revision.claims[0].text == "a better first claim"
    assert revision.claims[1:] == R1.claims[1:]  # untouched claims are byte-for-byte the same
    (change,) = revision.changes
    assert change.claim_id == "AN-1" and change.change is ChangeKind.MODIFIED
    assert change.caused_by == (CauseRef(reviewer_seat="skeptic", objection_index=0),)


def test_a_confidence_only_edit_is_weakened_or_strengthened_by_direction():
    down = convert(body(edits=[edit(confidence=0.2)]))
    up = convert(body(edits=[edit(confidence=0.9)]))
    assert down.changes[0].change is ChangeKind.WEAKENED
    assert up.changes[0].change is ChangeKind.STRENGTHENED
    status = convert(body(edits=[edit(status="uncertain", confidence=0.2)]))
    assert status.changes[0].change is ChangeKind.MODIFIED


def test_a_change_must_cite_an_objection_that_was_accepted():
    rejects(body(REJECT_BOTH, edits=[edit(text="x")]), "must cite an objection you accepted")
    rejects(
        body(edits=[edit(text="x", caused_by=[cause(objection=2)])]), "you accepted"
    )  # minor, unanswered
    rejects(body(edits=[edit(text="x", caused_by=[cause("creative")])]), "there is no objection")
    mixed = [ACCEPT_BOTH[0], {**ACCEPT_BOTH[1], "decision": "reject"}]
    rejects(body(mixed, edits=[edit(text="x", caused_by=[cause("fact_checker")])]), "you accepted")
    assert convert(body(mixed, edits=[edit(text="x", caused_by=[cause("skeptic")])]))


def test_an_edit_may_cite_several_objections():
    both = [cause("skeptic"), cause("fact_checker")]
    revision = convert(body(edits=[edit(text="x", caused_by=both)]))
    assert len(revision.changes[0].caused_by) == 2


def test_edits_must_name_one_of_the_seats_own_claims_once_and_change_something():
    rejects(body(edits=[edit(claim_id="SK-1", text="x")]), "your own claims")
    rejects(body(edits=[edit(claim_id=None, text="x")]), "your own claims")
    rejects(body(edits=[edit(text="x"), edit(text="y")]), "more than once")
    rejects(body(edits=[edit()]), "must change something")
    rejects(body(edits=[edit(text=R1.claims[0].text)]), "unchanged")


def test_a_retraction_removes_the_claim_and_carries_no_content():
    revision = convert(body(edits=[edit("retract", "AN-3", caused_by=[cause("fact_checker")])]))
    assert [c.id for c in revision.claims] == ["AN-1", "AN-2"]
    assert revision.changes[0].change is ChangeKind.RETRACTED
    rejects(
        body(edits=[edit("retract", "AN-3", text="x", caused_by=[cause("fact_checker")])]),
        "no new content",
    )


def test_a_claim_that_depends_on_a_retracted_claim_must_be_fixed_in_the_same_revision():
    rejects(body(edits=[edit("retract", "AN-1")]), "depends on AN-1")
    fixed = body(edits=[edit("retract", "AN-1"), edit("modify", "AN-2", depends_on=[])])
    fixed["edits"][1]["caused_by"] = [cause("skeptic")]
    assert [c.id for c in convert(fixed).claims] == ["AN-2", "AN-3"]


def test_a_new_claim_gets_the_next_id_and_a_retired_number_is_never_reused():
    added = {"text": "a new point", "status": "inferred", "confidence": 0.5}
    one = convert(body(edits=[add(**added)]))
    assert [c.id for c in one.claims] == ["AN-1", "AN-2", "AN-3", "AN-4"]
    assert one.changes[0].change is ChangeKind.ADDED and one.changes[0].claim_id == "AN-4"
    retire_then_add = convert(
        body(
            edits=[
                edit("retract", "AN-3", caused_by=[cause("fact_checker")]),
                add(**added),
                add(**{**added, "text": "another"}),
            ]
        )
    )
    assert [c.id for c in retire_then_add.claims] == ["AN-1", "AN-2", "AN-4", "AN-5"]


def test_a_new_claim_needs_content_and_takes_no_id():
    rejects(body(edits=[add(text="x", status="inferred")]), "needs confidence")
    rejects(
        body(edits=[edit("add", "AN-9", text="x", status="inferred", confidence=0.5)]),
        "takes no id",
    )


def test_a_revision_leaves_between_one_and_eight_claims():
    many = [add(text=f"claim {n}", status="inferred", confidence=0.5) for n in range(6)]
    rejects(body(edits=many), "between 1 and 8")
    everything = [edit("retract", f"AN-{n}") for n in (1, 2, 3)]
    rejects(body(edits=everything), "between 1 and 8")


# ── position and confidence ─────────────────────────────────────────────────


def test_position_and_confidence_change_only_with_an_accepted_objection():
    rejects(body(REJECT_BOTH, position="a new position"), "needs an accepted objection")
    rejects(body(REJECT_BOTH, confidence=0.9), "needs an accepted objection")
    revision = convert(body(position="a new position", confidence=0.4))
    assert revision.revised_position == "a new position" and revision.revised_confidence == 0.4
    assert revision.confidence_delta == pytest.approx(-0.15)


def test_saying_the_same_position_again_is_not_a_change():
    assert convert(body(REJECT_BOTH, position=R1.position)).status is RevisionStatus.REAFFIRMED


# ── no leaking a third perspective ──────────────────────────────────────────


def test_text_may_cite_the_seat_and_its_reviewers_but_not_anyone_else():
    ok = body(
        edits=[edit(text="AN-1 and SK-2 and FC-1 agree", reason="see SK-1")],
        remaining_uncertainties=["ISO-9 unknown"],
    )
    assert convert(ok)
    for field in (
        {"position": "as CR-1 argues"},
        {"remaining_disagreements": ["PR-2 says otherwise"]},
        {"edits": [edit(text="x", reason="CR-3 shows it")]},
        {"edits": [edit(text="x per PR-1")]},
    ):
        rejects(body(**field), "not one of the claims you were shown")
    rejects(body(responses=[{**ACCEPT_BOTH[0], "note": "CR-1"}, ACCEPT_BOTH[1]]), "not one of")


# ── determinism ─────────────────────────────────────────────────────────────


def test_conversion_is_deterministic_and_accepts_a_fenced_reply():
    payload = body(edits=[edit(text="x")], confidence=0.4)
    assert canonical_json(convert(payload)) == canonical_json(convert(payload))
    assert convert("```json\n" + json.dumps(payload) + "\n```")
    rejects(json.dumps(payload) + " thanks", "exactly one JSON object")
