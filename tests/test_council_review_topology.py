"""R2 topology: the approved review graph as profile data, and the per-seat reads it grants.

The graph is data the profile carries; the reads it grants go through ``StageView`` like every other
visibility rule, so a reviewer cannot take more than the graph gives it.
"""

from __future__ import annotations

import itertools

import pytest

from tests.council_core_fakes import FakeAssignments
from tests.council_scripted import PERSPECTIVE_SEATS, make_request, valid_perspective_json
from velune.council.domain import ArtifactKind, StageId
from velune.council.drafts import PerspectiveDraft, perspective_from_draft
from velune.council.profiles import (
    CODING_PROFILE,
    GENERAL_PROFILE,
    ReviewAssignment,
    RoleProfile,
)
from velune.council.stages import VisibilityPolicy
from velune.council.state import ArtifactStore, StageView, StoredArtifact, VisibilityViolation
from velune.council.topology import ProfileAssignments, coverage_after_failures, reviewers_of

K = ArtifactKind
APPROVED = {
    "analyst": (("skeptic", "fact_checker"), ()),
    "skeptic": (("analyst", "creative"), ()),
    "creative": (("skeptic", "practicalist"), ()),
    "practicalist": (("creative", "fact_checker"), ()),
    "fact_checker": ((), ("analyst", "skeptic", "creative", "practicalist")),
}


def graph(
    profile: RoleProfile = GENERAL_PROFILE,
) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    return {item.reviewer: (item.full, item.digest) for item in profile.review_graph}


# ── the data ────────────────────────────────────────────────────────────────


def test_the_general_graph_is_exactly_the_approved_one():
    assert graph() == APPROVED
    assert [item.reviewer for item in GENERAL_PROFILE.review_graph] == [
        "analyst",
        "skeptic",
        "creative",
        "fact_checker",
        "practicalist",
    ]


def test_there_is_no_self_review_and_every_target_is_a_perspective_seat():
    for reviewer, (full, digest) in graph().items():
        assert reviewer not in full + digest
        assert set(full + digest) <= set(PERSPECTIVE_SEATS)


def test_four_seats_read_two_peers_in_full_and_the_fact_checker_takes_a_digest_of_all_four():
    for reviewer, (full, digest) in graph().items():
        if reviewer == "fact_checker":
            assert full == () and set(digest) == set(PERSPECTIVE_SEATS) - {reviewer}
        else:
            assert len(full) == 2 and digest == ()


def test_full_review_in_degree_is_the_designed_one():
    counts = dict.fromkeys(PERSPECTIVE_SEATS, 0)
    for full, _ in graph().values():
        for target in full:
            counts[target] += 1
    assert counts == {
        "analyst": 1,
        "skeptic": 2,
        "creative": 2,
        "fact_checker": 2,
        "practicalist": 1,
    }
    assert sum(counts.values()) == 8


def test_every_seat_has_at_least_two_distinct_reviewers():
    for seat in PERSPECTIVE_SEATS:
        assert len(set(reviewers_of(GENERAL_PROFILE, seat))) >= 2


def test_role_opposed_pairs_review_each_other():
    full = {r: set(f) for r, (f, _) in graph().items()}
    for a, b in (("analyst", "skeptic"), ("skeptic", "creative"), ("creative", "practicalist")):
        assert b in full[a] and a in full[b]


def test_losing_any_one_reviewer_leaves_every_seat_with_a_critique():
    for failed in PERSPECTIVE_SEATS:
        left = coverage_after_failures(GENERAL_PROFILE, frozenset({failed}))
        assert all(count >= 1 for count in left.values()), (failed, left)


def test_the_fact_checker_dependency_for_analyst_and_practicalist_is_the_known_weak_spot():
    left = coverage_after_failures(GENERAL_PROFILE, frozenset({"fact_checker"}))
    assert left["analyst"] == 1 and left["practicalist"] == 1
    assert left["skeptic"] == 2 and left["creative"] == 2


def test_other_profiles_carry_no_graph_and_stay_unrunnable():
    assert CODING_PROFILE.review_graph == () and CODING_PROFILE.runnable is False


def test_review_for_returns_the_assignment_or_none():
    assert GENERAL_PROFILE.review_for("analyst") == ReviewAssignment(
        reviewer="analyst", full=("skeptic", "fact_checker")
    )
    assert GENERAL_PROFILE.review_for("moderator") is None


# ── profile validation ──────────────────────────────────────────────────────


def with_graph(*items: ReviewAssignment) -> RoleProfile:
    return GENERAL_PROFILE.model_validate(
        {**GENERAL_PROFILE.model_dump(), "review_graph": [i.model_dump() for i in items]}
    )


@pytest.mark.parametrize(
    "bad",
    [
        (ReviewAssignment(reviewer="analyst", full=("analyst",)),),  # self
        (ReviewAssignment(reviewer="moderator", full=("analyst",)),),  # not a perspective seat
        (ReviewAssignment(reviewer="analyst", full=("moderator",)),),  # target not a perspective
        (ReviewAssignment(reviewer="analyst", full=("skeptic",), digest=("skeptic",)),),  # twice
        (
            ReviewAssignment(reviewer="analyst", full=("skeptic",)),
            ReviewAssignment(reviewer="analyst", full=("creative",)),
        ),  # listed twice
    ],
)
def test_a_malformed_graph_is_rejected(bad):
    with pytest.raises(ValueError):
        with_graph(*bad)


# ── the reads the graph grants ──────────────────────────────────────────────


def perspective(seat_id: str):
    spec = GENERAL_PROFILE.seat(seat_id)
    draft = PerspectiveDraft.model_validate_json(valid_perspective_json(seat_id, "t"))
    return perspective_from_draft(draft, seat=spec)


def full_store(seats=PERSPECTIVE_SEATS) -> ArtifactStore:
    store = ArtifactStore()
    store.commit(
        tuple(
            StoredArtifact(
                kind=K.PERSPECTIVE,
                stage=StageId.PERSPECTIVES,
                author=seat,
                payload=perspective(seat),
            )
            for seat in seats
        )
    )
    return store


def view_of(viewer: str, *, stage=StageId.REVIEW, assignments=None, store=None) -> StageView:
    return StageView(
        policy=VisibilityPolicy(),
        stage=stage,
        viewer=viewer,
        request=make_request(),
        store=store or full_store(),
        assignments=assignments or ProfileAssignments(GENERAL_PROFILE),
    )


def authors(items) -> list[str]:
    return [item.author for item in items]


@pytest.mark.parametrize("reviewer", [s for s in PERSPECTIVE_SEATS if s != "fact_checker"])
def test_a_full_reviewer_reads_itself_and_exactly_its_two_peers_and_no_digest(reviewer):
    view = view_of(reviewer)
    full, _ = APPROVED[reviewer]
    assert sorted(authors(view.read(K.PERSPECTIVE))) == sorted((reviewer, *full))
    assert view.digest(K.PERSPECTIVE) == ()  # the contract grants a digest; the graph scopes it
    for other in set(PERSPECTIVE_SEATS) - {reviewer, *full}:
        with pytest.raises(VisibilityViolation):
            view.read(K.PERSPECTIVE, seat=other)


def test_the_fact_checker_reads_only_itself_in_full_and_the_other_four_as_a_digest():
    view = view_of("fact_checker")
    assert authors(view.read(K.PERSPECTIVE)) == ["fact_checker"]
    digest = view.digest(K.PERSPECTIVE)
    assert sorted(d.author for d in digest) == ["analyst", "creative", "practicalist", "skeptic"]
    for peer in PERSPECTIVE_SEATS:
        if peer != "fact_checker":
            with pytest.raises(VisibilityViolation):
                view.read(K.PERSPECTIVE, seat=peer)


def test_the_digest_is_claims_only():
    dumped = repr(view_of("fact_checker").digest(K.PERSPECTIVE))
    assert "SENT-ANALYST-t" in dumped  # the claim text
    assert "rationale" not in dumped and "position" not in dumped


def test_absent_peers_are_simply_absent_never_replaced():
    store = full_store(("analyst", "creative", "fact_checker"))
    skeptic_missing = view_of("analyst", store=store)
    assert authors(skeptic_missing.read(K.PERSPECTIVE)) == ["analyst", "fact_checker"]
    fc = view_of("fact_checker", store=store)
    assert sorted(d.author for d in fc.digest(K.PERSPECTIVE)) == ["analyst", "creative"]


def test_other_stages_get_no_review_reads_and_keep_their_unscoped_digest():
    assignments = ProfileAssignments(GENERAL_PROFILE)
    assert assignments.assigned_authors(StageId.REVISION, "analyst") == ()
    assert assignments.digest_authors(StageId.SYNTHESIS, "synthesizer") is None
    synth = view_of("synthesizer", stage=StageId.SYNTHESIS)
    assert len(synth.digest(K.PERSPECTIVE)) == 5  # R5 keeps its digest of everyone


def test_an_assignment_source_without_digest_scoping_leaves_the_digest_unrestricted():
    unscoped = FakeAssignments({(StageId.REVIEW, "analyst"): ("skeptic",)})
    view = view_of("analyst", assignments=unscoped)
    assert len(view.digest(K.PERSPECTIVE)) == 5  # Phase 1 behaviour, unchanged


def test_a_reviewer_with_no_graph_entry_reads_nothing_extra():
    view = view_of("moderator")
    assert view.digest(K.PERSPECTIVE) == ()
    assert view.read(K.PERSPECTIVE) == ()


def test_every_graph_edge_is_a_read_and_nothing_more():
    """Across all reviewers, the full reads granted are exactly the graph's edges."""
    granted = set()
    for reviewer in PERSPECTIVE_SEATS:
        view = view_of(reviewer)
        granted |= {(reviewer, a) for a in authors(view.read(K.PERSPECTIVE)) if a != reviewer} | {
            (reviewer, d.author) for d in view.digest(K.PERSPECTIVE)
        }
    expected = {(r, t) for r, (f, d) in APPROVED.items() for t in f + d}
    assert granted == expected
    assert len(list(itertools.combinations(PERSPECTIVE_SEATS, 2))) > len(granted) / 2
