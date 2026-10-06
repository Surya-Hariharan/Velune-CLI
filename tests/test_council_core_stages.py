"""Stage contracts are data, and StageView/VisibilityPolicy enforce them."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.council_core_fakes import FakeAssignments
from velune.council.contracts import Claim, Critique, Perspective
from velune.council.domain import (
    STAGE_ORDER,
    ArtifactKind,
    Criticality,
    Depth,
    QuorumRule,
    ReadScope,
    SeatKind,
    StageId,
)
from velune.council.request import CouncilRequest, EvidenceItem
from velune.council.stages import (
    STAGE_CONTRACTS,
    StageContract,
    StagePlan,
    VisibilityPolicy,
)
from velune.council.state import (
    ArtifactStore,
    StageView,
    StoredArtifact,
    VisibilityViolation,
)

K, S, ID = ArtifactKind, ReadScope, StageId

# The visibility matrix of the target design, written out independently of the contracts.
EXPECTED_MATRIX = {
    ID.FRAME: {K.QUESTION: (S.ALL,)},
    ID.PERSPECTIVES: {K.QUESTION: (S.ALL,), K.FRAME: (S.ALL,), K.EVIDENCE: (S.ALL,)},
    ID.REVIEW: {
        K.QUESTION: (S.ALL,),
        K.FRAME: (S.ALL,),
        K.EVIDENCE: (S.ALL,),
        K.PERSPECTIVE: (S.OWN, S.ASSIGNED, S.DIGEST_ALL),
    },
    ID.REVISION: {
        K.QUESTION: (S.ALL,),
        K.FRAME: (S.ALL,),
        K.EVIDENCE: (S.ALL,),
        K.PERSPECTIVE: (S.OWN,),
        K.CRITIQUE: (S.ADDRESSED_TO_SELF,),
    },
    ID.ARBITRATION: {
        K.QUESTION: (S.ALL,),
        K.FRAME: (S.ALL,),
        K.EVIDENCE: (S.ALL,),
        K.PERSPECTIVE: (S.ALL,),
        K.CRITIQUE: (S.ALL,),
        K.REVISION: (S.ALL,),
    },
    ID.SYNTHESIS: {
        K.QUESTION: (S.ALL,),
        K.FRAME: (S.ALL,),
        K.DECISION: (S.ALL,),
        K.PERSPECTIVE: (S.DIGEST_ALL,),
        K.REVISION: (S.DIGEST_ALL,),
    },
}


def test_contracts_declare_every_stage_in_order():
    assert tuple(STAGE_CONTRACTS) == STAGE_ORDER
    assert all(c.stage is stage for stage, c in STAGE_CONTRACTS.items())


def test_visibility_matrix_equals_the_declared_design():
    assert VisibilityPolicy().matrix() == EXPECTED_MATRIX


def test_independence_is_a_contract_r1_cannot_read_any_peer_artifact():
    policy = VisibilityPolicy()
    for kind in (K.PERSPECTIVE, K.CRITIQUE, K.REVISION, K.DECISION, K.ANSWER):
        assert not policy.can_read(ID.PERSPECTIVES, kind)
    assert K.PERSPECTIVE in {r.kind for r in STAGE_CONTRACTS[ID.PERSPECTIVES].reads}
    assert (
        next(r for r in STAGE_CONTRACTS[ID.PERSPECTIVES].reads if r.kind is K.PERSPECTIVE).scope
        is S.NONE
    )


def test_frame_stage_sees_only_the_question():
    policy = VisibilityPolicy()
    assert [k for k in ArtifactKind if policy.can_read(ID.FRAME, k)] == [K.QUESTION]


def test_synthesis_never_sees_raw_critiques_or_full_perspectives():
    policy = VisibilityPolicy()
    assert not policy.can_read(ID.SYNTHESIS, K.CRITIQUE)
    assert policy.scopes(ID.SYNTHESIS, K.PERSPECTIVE) == (S.DIGEST_ALL,)


def test_each_stage_writes_one_kind_and_acts_with_the_right_seat_kind():
    assert {s: c.writes for s, c in STAGE_CONTRACTS.items()} == {
        ID.FRAME: K.FRAME,
        ID.PERSPECTIVES: K.PERSPECTIVE,
        ID.REVIEW: K.CRITIQUE,
        ID.REVISION: K.REVISION,
        ID.ARBITRATION: K.DECISION,
        ID.SYNTHESIS: K.ANSWER,
    }
    assert STAGE_CONTRACTS[ID.FRAME].seat_kinds == (SeatKind.MODERATOR,)
    assert STAGE_CONTRACTS[ID.ARBITRATION].seat_kinds == (SeatKind.ARBITRATOR,)
    assert STAGE_CONTRACTS[ID.SYNTHESIS].seat_kinds == (SeatKind.SYNTHESIZER,)
    assert STAGE_CONTRACTS[ID.PERSPECTIVES].seat_kinds == (SeatKind.PERSPECTIVE,)


def test_criticality_and_parallelism():
    required = {s for s, c in STAGE_CONTRACTS.items() if c.criticality is Criticality.REQUIRED}
    assert required == {ID.PERSPECTIVES, ID.ARBITRATION, ID.SYNTHESIS}
    parallel = {s for s, c in STAGE_CONTRACTS.items() if c.parallel}
    assert parallel == {ID.PERSPECTIVES, ID.REVIEW, ID.REVISION}
    assert STAGE_CONTRACTS[ID.PERSPECTIVES].quorum_from_profile is True


def test_budget_shares_sum_to_one():
    assert sum(c.budget_share for c in STAGE_CONTRACTS.values()) == pytest.approx(1.0)


def test_contract_rejects_two_quorum_sources_and_bad_budget():
    base = {
        "stage": ID.PERSPECTIVES,
        "seat_kinds": (SeatKind.PERSPECTIVE,),
        "reads": (),
        "writes": K.PERSPECTIVE,
        "parallel": True,
        "criticality": Criticality.REQUIRED,
        "budget_share": 0.5,
    }
    with pytest.raises(ValidationError):
        StageContract(**base, quorum=QuorumRule(min_ok=1), quorum_from_profile=True)
    with pytest.raises(ValidationError):
        StageContract(**{**base, "budget_share": 0})
    with pytest.raises(ValidationError):
        StageContract(**{**base, "seat_kinds": ()})


def test_plan_per_depth():
    assert StagePlan.for_depth(Depth.STANDARD) == STAGE_ORDER
    assert StagePlan.for_depth(Depth.QUICK) == (
        ID.FRAME,
        ID.PERSPECTIVES,
        ID.ARBITRATION,
        ID.SYNTHESIS,
    )
    assert {d.value for d in Depth} == {"quick", "standard"}


# ── StageView enforcement ───────────────────────────────────────────────────


def claim(cid: str, text: str = "t") -> Claim:
    return Claim(id=cid, text=text, status="known", confidence=0.5)


def perspective(seat: str, prefix: str, text: str = "secret") -> Perspective:
    return Perspective(
        seat_id=seat,
        position=f"{seat} position",
        claims=(claim(f"{prefix}-1", text),),
        confidence=0.5,
        rationale=f"{seat} rationale",
    )


def critique(reviewer: str, target: str) -> Critique:
    return Critique(
        reviewer_seat=reviewer, target_seat=target, steelman="best point", confidence=0.5
    )


def stored(kind, author, payload, stage=ID.PERSPECTIVES, target=None) -> StoredArtifact:
    return StoredArtifact(kind=kind, stage=stage, author=author, target=target, payload=payload)


REQUEST = CouncilRequest(
    request_id="r",
    question="Why?",
    context="ctx",
    evidence=(EvidenceItem(id="e1", text="fact"),),
)


@pytest.fixture
def store() -> ArtifactStore:
    s = ArtifactStore()
    s.commit(
        (
            stored(K.PERSPECTIVE, "analyst", perspective("analyst", "AN", "analyst-secret")),
            stored(K.PERSPECTIVE, "skeptic", perspective("skeptic", "SK", "skeptic-secret")),
            stored(K.PERSPECTIVE, "creative", perspective("creative", "CR", "creative-secret")),
            stored(
                K.CRITIQUE,
                "skeptic",
                critique("skeptic", "analyst"),
                stage=ID.REVIEW,
                target="analyst",
            ),
            stored(
                K.CRITIQUE,
                "creative",
                critique("creative", "analyst"),
                stage=ID.REVIEW,
                target="analyst",
            ),
            stored(
                K.CRITIQUE,
                "analyst",
                critique("analyst", "skeptic"),
                stage=ID.REVIEW,
                target="skeptic",
            ),
        )
    )
    return s


def view(stage: StageId, viewer: str, store: ArtifactStore, assignments=None) -> StageView:
    return StageView(
        policy=VisibilityPolicy(),
        stage=stage,
        viewer=viewer,
        request=REQUEST,
        store=store,
        assignments=assignments,
    )


def authors(items) -> list[str]:
    return [i.author for i in items]


def test_request_data_is_gated_by_the_contract(store):
    frame_view = view(ID.FRAME, "moderator", store)
    assert frame_view.question() == "Why?"
    with pytest.raises(VisibilityViolation):
        frame_view.evidence()  # R0 has no evidence read
    r1 = view(ID.PERSPECTIVES, "analyst", store)
    assert r1.evidence()[0].id == "e1" and r1.context() == "ctx"
    with pytest.raises(VisibilityViolation):
        view(ID.SYNTHESIS, "synthesizer", store).evidence()


@pytest.mark.parametrize("kind", [K.PERSPECTIVE, K.CRITIQUE, K.REVISION, K.DECISION])
def test_r1_seat_cannot_read_peers_by_any_route(store, kind):
    r1 = view(ID.PERSPECTIVES, "analyst", store)
    with pytest.raises(VisibilityViolation):
        r1.read(kind)
    with pytest.raises(VisibilityViolation):
        r1.read(kind, seat="skeptic")
    with pytest.raises(VisibilityViolation):
        r1.digest(kind)


def test_own_scope_returns_only_the_viewers_artifacts_and_blocks_others(store):
    r3 = view(ID.REVISION, "analyst", store)
    assert authors(r3.read(K.PERSPECTIVE)) == ["analyst"]
    assert authors(r3.read(K.PERSPECTIVE, seat="analyst")) == ["analyst"]
    with pytest.raises(VisibilityViolation):
        r3.read(K.PERSPECTIVE, seat="skeptic")


def test_addressed_to_self_returns_only_critiques_aimed_at_the_viewer(store):
    r3 = view(ID.REVISION, "analyst", store)
    got = r3.read(K.CRITIQUE)
    assert sorted(authors(got)) == ["creative", "skeptic"]
    assert all(item.target == "analyst" for item in got)
    only_skeptic = r3.read(K.CRITIQUE, seat="skeptic")
    assert authors(only_skeptic) == ["skeptic"]
    # skeptic's own revision sees only the critique aimed at skeptic
    assert authors(view(ID.REVISION, "skeptic", store).read(K.CRITIQUE)) == ["analyst"]
    # a seat nobody reviewed sees nothing
    assert view(ID.REVISION, "creative", store).read(K.CRITIQUE) == ()


def test_r3_cannot_read_other_revisions_or_arbitration(store):
    r3 = view(ID.REVISION, "analyst", store)
    for kind in (K.REVISION, K.DECISION, K.ANSWER):
        with pytest.raises(VisibilityViolation):
            r3.read(kind)


def test_assigned_scope_uses_the_assignment_source(store):
    graph = FakeAssignments({(ID.REVIEW, "analyst"): ("skeptic",)})
    r2 = view(ID.REVIEW, "analyst", store, graph)
    assert authors(r2.read(K.PERSPECTIVE)) == ["analyst", "skeptic"]  # own + assigned
    assert authors(r2.read(K.PERSPECTIVE, seat="skeptic")) == ["skeptic"]
    with pytest.raises(VisibilityViolation):
        r2.read(K.PERSPECTIVE, seat="creative")  # neither own nor assigned


def test_without_an_assignment_source_assigned_scope_grants_nothing_extra(store):
    r2 = view(ID.REVIEW, "analyst", store)
    assert authors(r2.read(K.PERSPECTIVE)) == ["analyst"]
    with pytest.raises(VisibilityViolation):
        r2.read(K.PERSPECTIVE, seat="skeptic")


def test_digest_returns_claims_only_never_prose(store):
    r2 = view(ID.REVIEW, "fact_checker", store)
    digests = r2.digest(K.PERSPECTIVE)
    assert [d.author for d in digests] == ["analyst", "skeptic", "creative"]
    dumped = repr(digests)
    assert "analyst-secret" in dumped  # the claim text is the digest
    assert "rationale" not in dumped and "position" not in dumped


def test_digest_only_access_cannot_read_full_artifacts(store):
    synth = view(ID.SYNTHESIS, "synthesizer", store)
    with pytest.raises(VisibilityViolation):
        synth.read(K.PERSPECTIVE)
    assert len(synth.digest(K.PERSPECTIVE)) == 3
    with pytest.raises(VisibilityViolation):
        synth.digest(K.CRITIQUE)


def test_digest_requires_a_digest_grant(store):
    with pytest.raises(VisibilityViolation):
        view(ID.REVISION, "analyst", store).digest(K.PERSPECTIVE)  # OWN only


def test_arbitrator_reads_everything_it_is_granted(store):
    r4 = view(ID.ARBITRATION, "arbitrator", store)
    assert len(r4.read(K.PERSPECTIVE)) == 3
    assert len(r4.read(K.CRITIQUE)) == 3
    assert r4.read(K.REVISION) == ()
    with pytest.raises(VisibilityViolation):
        r4.read(K.ANSWER)


def test_request_kinds_must_use_the_accessors(store):
    with pytest.raises(VisibilityViolation):
        view(ID.PERSPECTIVES, "analyst", store).read(K.QUESTION)
    with pytest.raises(VisibilityViolation):
        view(ID.PERSPECTIVES, "analyst", store).read(K.EVIDENCE)


def test_every_denied_cell_of_the_matrix_raises(store):
    """Exhaustive: for every stage and kind the contract does not grant, reading is a violation."""
    policy = VisibilityPolicy()
    denied = 0
    for stage in STAGE_ORDER:
        for kind in ArtifactKind:
            if kind in (K.QUESTION, K.EVIDENCE) or policy.can_read(stage, kind):
                continue
            with pytest.raises(VisibilityViolation):
                view(stage, "analyst", store).read(kind)
            denied += 1
    assert denied > 20


def test_store_is_append_only_and_kinds_are_filtered():
    s = ArtifactStore()
    assert len(s) == 0
    s.commit((stored(K.PERSPECTIVE, "analyst", perspective("analyst", "AN")),))
    assert len(s) == 1 and s.all(K.CRITIQUE) == ()
    assert not hasattr(s, "remove") and not hasattr(s, "clear")
