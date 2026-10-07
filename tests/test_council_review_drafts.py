"""R2 wire drafts: strict shape, core-owned identity, and the checks that make a critique usable."""

from __future__ import annotations

import json

import pytest

from tests.council_scripted import claim_ids_of, valid_review_json
from velune.council.parsing import DraftParseError, parse_and_convert
from velune.council.profiles import GENERAL_PROFILE
from velune.council.reviewdrafts import (
    ReviewReplyDraft,
    critiques_from_draft,
    foreign_claim_ids,
    profile_prefixes,
)
from velune.council.serialization import canonical_json

ANALYST = GENERAL_PROFILE.seat("analyst")
FC = GENERAL_PROFILE.seat("fact_checker")
ASSIGNED = ("skeptic", "fact_checker")


def shown_for(*targets):
    return {t: claim_ids_of(t) for t in targets}


def convert(text, reviewer=ANALYST, targets=ASSIGNED):
    return parse_and_convert(
        text,
        ReviewReplyDraft,
        lambda draft: critiques_from_draft(
            draft, reviewer=reviewer, shown=shown_for(*targets), profile=GENERAL_PROFILE
        ),
    )


def reply(**review_overrides):
    body = json.loads(valid_review_json("analyst", ASSIGNED))
    for review in body["reviews"]:
        review.update(review_overrides)
    return json.dumps(body)


def rejects(text, fragment=""):
    with pytest.raises(DraftParseError) as info:
        convert(text)
    assert fragment in " ".join(info.value.errors)


# ── the happy path ──────────────────────────────────────────────────────────


def test_a_valid_reply_becomes_one_critique_per_target_with_core_owned_identity():
    critiques = convert(valid_review_json("analyst", ASSIGNED))
    assert [(c.reviewer_seat, c.target_seat) for c in critiques] == [
        ("analyst", "skeptic"),
        ("analyst", "fact_checker"),
    ]
    first = critiques[0]
    assert first.disagreements[0].claim_id == "SK-1" and first.agreements == ("SK-2",)
    assert first.no_material_issues is False and first.schema_version == 1


def test_critiques_come_back_in_profile_order_whatever_order_the_model_used():
    forward = convert(valid_review_json("analyst", ("skeptic", "fact_checker")))
    backward = convert(valid_review_json("analyst", ("fact_checker", "skeptic")))
    assert [c.target_seat for c in forward] == [c.target_seat for c in backward]
    assert canonical_json(forward) == canonical_json(backward)


def test_a_review_that_finds_nothing_says_so_explicitly():
    text = reply(disagreements=[], no_material_issues=True)
    assert all(c.no_material_issues and not c.disagreements for c in convert(text))


def test_the_fact_checker_digest_reply_covers_all_four_peers():
    peers = ("analyst", "skeptic", "creative", "practicalist")
    text = valid_review_json("fact_checker", peers)
    critiques = convert(text, reviewer=FC, targets=peers)
    assert [c.target_seat for c in critiques] == list(peers)
    assert {c.reviewer_seat for c in critiques} == {"fact_checker"}


def test_conversion_is_deterministic():
    text = valid_review_json("analyst", ASSIGNED, "x")
    assert canonical_json(convert(text)) == canonical_json(convert(text))


def test_a_fenced_reply_is_accepted_and_trailing_prose_is_not():
    text = valid_review_json("analyst", ASSIGNED)
    assert convert(f"```json\n{text}\n```")
    rejects(text + "\nHope that helps!", "exactly one JSON object")


# ── strict shape: nothing the model should not set ──────────────────────────


@pytest.mark.parametrize(
    "extra",
    [
        "reviewer_seat",
        "reviewer",
        "target_seat",
        "schema_version",
        "reasoning",
        "thoughts",
        "scratchpad",
        "chain_of_thought",
        "claim_verdicts",
        "peer_influence",
    ],
)
def test_unknown_fields_are_rejected_at_every_level(extra):
    body = json.loads(valid_review_json("analyst", ASSIGNED))
    top = {**body, extra: "x"}
    rejects(json.dumps(top))
    inner = json.loads(json.dumps(body))
    inner["reviews"][0][extra] = "x"
    rejects(json.dumps(inner))
    deep = json.loads(json.dumps(body))
    deep["reviews"][0]["disagreements"][0][extra] = "x"
    rejects(json.dumps(deep))


def test_size_caps_hold():
    body = json.loads(valid_review_json("analyst", ASSIGNED))
    body["reviews"][0]["steelman"] = "x" * 401
    rejects(json.dumps(body))
    body = json.loads(valid_review_json("analyst", ASSIGNED))
    body["reviews"][0]["agreements"] = ["SK-1"] * 9
    rejects(json.dumps(body))
    body = json.loads(valid_review_json("analyst", ASSIGNED))
    body["reviews"][0]["disagreements"] = body["reviews"][0]["disagreements"] * 7
    rejects(json.dumps(body))
    rejects(json.dumps({"reviews": []}))


def test_unknown_kinds_severities_and_bad_claim_ids_are_rejected():
    for field, value in (("kind", "rant"), ("severity", "apocalyptic"), ("claim_id", "sk-1")):
        body = json.loads(valid_review_json("analyst", ASSIGNED))
        body["reviews"][0]["disagreements"][0][field] = value
        rejects(json.dumps(body))
    body = json.loads(valid_review_json("analyst", ASSIGNED))
    body["reviews"][0]["confidence"] = 1.5
    rejects(json.dumps(body))


# ── identity: the model cannot choose who reviews whom ──────────────────────


def test_every_assigned_target_must_be_reviewed_exactly_once():
    rejects(valid_review_json("analyst", ("skeptic",)), "expected exactly one review")
    rejects(valid_review_json("analyst", ("skeptic", "fact_checker", "creative")), "exactly one")
    rejects(valid_review_json("analyst", ("skeptic", "skeptic")), "only once")


def test_a_seat_cannot_review_itself_or_an_unassigned_peer():
    rejects(valid_review_json("analyst", ("analyst", "skeptic")), "exactly one")
    rejects(valid_review_json("analyst", ("skeptic", "practicalist")), "exactly one")


def test_a_review_may_cite_only_claims_of_the_target_it_names():
    body = json.loads(valid_review_json("analyst", ASSIGNED))
    body["reviews"][0]["disagreements"][0]["claim_id"] = "FC-1"  # a different target's claim
    rejects(json.dumps(body), "does not belong to skeptic")
    body = json.loads(valid_review_json("analyst", ASSIGNED))
    body["reviews"][0]["agreements"] = ["SK-9"]  # no such claim was shown
    rejects(json.dumps(body), "does not belong")


def test_swapped_labels_are_caught_by_the_ids_they_cite():
    body = json.loads(valid_review_json("analyst", ASSIGNED))
    body["reviews"][0]["target"], body["reviews"][1]["target"] = "fact_checker", "skeptic"
    rejects(json.dumps(body), "does not belong")


# ── a review must say something ─────────────────────────────────────────────


def test_a_review_with_neither_objections_nor_the_no_issues_flag_is_rejected():
    rejects(reply(disagreements=[], no_material_issues=False), "give a disagreement")


def test_the_no_issues_flag_cannot_come_with_objections():
    rejects(reply(no_material_issues=True), "cannot come with")


# ── no leaking a third perspective through free text ────────────────────────


def with_text(field_text):
    body = json.loads(valid_review_json("analyst", ASSIGNED))
    field_text(body["reviews"][0])
    return json.dumps(body)


def test_text_that_cites_another_seats_claim_is_rejected():
    for edit in (
        lambda r: r.update(steelman="Like CR-2 says, this holds"),
        lambda r: r["disagreements"][0].update(objection="Contradicts PR-1 directly"),
        lambda r: r["disagreements"][0].update(suggested_resolution="See FC-3"),
    ):
        rejects(with_text(edit), "not this target's claim")


def test_text_may_cite_the_target_and_the_reviewer_and_look_like_other_ids():
    ok = with_text(
        lambda r: r["disagreements"][0].update(
            objection="SK-3 and my own AN-1 differ; see ISO-9 and UTF-8"
        )
    )
    assert convert(ok)


def test_foreign_claim_ids_helper():
    known = profile_prefixes(GENERAL_PROFILE)
    assert foreign_claim_ids(["AN-1 and SK-2"], allowed={"AN"}, known=known) == ["SK-2"]
    assert foreign_claim_ids(["ISO-9 UTF-8 AN-1000"], allowed=set(), known=known) == []
    assert foreign_claim_ids([""], allowed=set(), known=known) == []
