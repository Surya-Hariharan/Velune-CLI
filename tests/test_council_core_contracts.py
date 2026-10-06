"""Contracts are strict: caps, enums, id formats, immutability and no room for hidden reasoning."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from velune.council.contracts import (
    ArbitrationResult,
    Artifact,
    Claim,
    ClaimStatus,
    Cluster,
    Critique,
    Frame,
    Perspective,
    Revision,
)
from velune.council.request import CouncilRequest, CouncilSettings, EvidenceItem


def claim(cid: str = "AN-1", **kw) -> Claim:
    base = {"id": cid, "text": "A claim", "status": "known", "confidence": 0.5}
    base.update(kw)
    return Claim(**base)


def perspective(**kw) -> Perspective:
    base = {
        "seat_id": "analyst",
        "position": "A position",
        "claims": (claim(),),
        "confidence": 0.7,
        "rationale": "Because",
    }
    base.update(kw)
    return Perspective(**base)


def critique(**kw) -> Critique:
    base = {
        "reviewer_seat": "skeptic",
        "target_seat": "analyst",
        "steelman": "Their best point",
        "confidence": 0.6,
    }
    base.update(kw)
    return Critique(**base)


def revision(**kw) -> Revision:
    base = {
        "seat_id": "analyst",
        "status": "reaffirmed",
        "revised_position": "Same",
        "claims": (claim(),),
        "revised_confidence": 0.7,
        "confidence_delta": 0.0,
    }
    base.update(kw)
    return Revision(**base)


def frame(**kw) -> Frame:
    base = {"question_restated": "What?", "problem_type": "explanation"}
    base.update(kw)
    return Frame(**base)


def arbitration(**kw) -> ArbitrationResult:
    base = {"overall_confidence": 0.6, "confidence_rationale": "Mixed evidence"}
    base.update(kw)
    return ArbitrationResult(**base)


ALL_BUILDERS = [claim, perspective, critique, revision, frame, arbitration]


@pytest.mark.parametrize("build", ALL_BUILDERS, ids=lambda b: b.__name__)
@pytest.mark.parametrize("hidden", ["reasoning", "thoughts", "scratchpad", "chain_of_thought"])
def test_hidden_reasoning_fields_are_rejected(build, hidden):
    with pytest.raises(ValidationError):
        build(**{hidden: "secret"})


@pytest.mark.parametrize("build", ALL_BUILDERS, ids=lambda b: b.__name__)
def test_contracts_are_immutable(build):
    obj = build()
    field = next(iter(type(obj).model_fields))
    with pytest.raises(ValidationError):
        setattr(obj, field, getattr(obj, field))


def test_collections_are_tuples_not_lists():
    p = perspective(claims=[claim("AN-1"), claim("AN-2")])
    assert isinstance(p.claims, tuple)
    assert isinstance(p.assumptions, tuple)
    hash(claim())  # frozen models of tuples are hashable


@pytest.mark.parametrize("bad", ["an-1", "A-1", "ANAL-1", "AN1", "AN-", "AN-1234", ""])
def test_claim_id_format(bad):
    with pytest.raises(ValidationError):
        claim(bad)


@pytest.mark.parametrize("good", ["AN-1", "SK-12", "ABC-999"])
def test_claim_id_format_accepts(good):
    assert claim(good).id == good


def test_claim_caps_and_enums():
    with pytest.raises(ValidationError):
        claim(text="x" * 241)
    with pytest.raises(ValidationError):
        claim(text="")
    with pytest.raises(ValidationError):
        claim(support="x" * 201)
    with pytest.raises(ValidationError):
        claim(status="maybe")
    with pytest.raises(ValidationError):
        claim(confidence=1.01)
    with pytest.raises(ValidationError):
        claim(confidence=-0.1)
    assert claim(status=ClaimStatus.INFERRED).status is ClaimStatus.INFERRED


def test_confidence_is_rounded_at_the_boundary():
    assert claim(confidence=0.123456789).confidence == 0.1235


def test_perspective_bounds():
    with pytest.raises(ValidationError):
        perspective(claims=())
    with pytest.raises(ValidationError):
        perspective(claims=tuple(claim(f"AN-{i}") for i in range(1, 10)))
    with pytest.raises(ValidationError):
        perspective(claims=(claim("AN-1"), claim("AN-1")))
    with pytest.raises(ValidationError):
        perspective(position="x" * 401)
    with pytest.raises(ValidationError):
        perspective(rationale="x" * 601)
    with pytest.raises(ValidationError):
        perspective(seat_id="Not A Slug")


def test_frame_limits_dimensions_and_validates_slug():
    assert frame(dimensions=tuple(f"d{i}" for i in range(6))).dimensions
    with pytest.raises(ValidationError):
        frame(dimensions=tuple(f"d{i}" for i in range(7)))
    with pytest.raises(ValidationError):
        frame(problem_type="Not A Slug")
    assert frame().degraded is False


def test_critique_cannot_target_its_own_seat():
    with pytest.raises(ValidationError):
        critique(reviewer_seat="analyst", target_seat="analyst")
    assert critique().no_material_issues is False


def test_critique_nested_models_are_strict():
    ok = critique(
        disagreements=[
            {
                "claim_id": "AN-1",
                "objection": "Too strong",
                "kind": "unsupported",
                "severity": "major",
            }
        ]
    )
    assert ok.disagreements[0].severity.value == "major"
    with pytest.raises(ValidationError):
        critique(
            disagreements=[
                {
                    "claim_id": "AN-1",
                    "objection": "x",
                    "kind": "unsupported",
                    "severity": "severe",
                }
            ]
        )
    with pytest.raises(ValidationError):
        critique(
            disagreements=[
                {
                    "claim_id": "AN-1",
                    "objection": "x",
                    "kind": "unsupported",
                    "severity": "minor",
                    "reasoning": "hidden",
                }
            ]
        )


def test_revision_status_and_delta_bounds():
    assert revision(status="carried_forward").status.value == "carried_forward"
    with pytest.raises(ValidationError):
        revision(status="rewritten")
    with pytest.raises(ValidationError):
        revision(confidence_delta=1.5)


def test_arbitration_result_defaults_are_empty_not_approving():
    result = arbitration()
    assert result.clusters == ()
    assert result.consensus_cluster_ids == ()
    assert result.degraded is False
    assert result.schema_version == 1


def test_cluster_requires_members_and_valid_id():
    base = {
        "cluster_id": "C1",
        "canonical_text": "t",
        "member_claim_ids": ("AN-1",),
        "verdict": "upheld",
        "decision_basis": "b",
        "evidence_quality": "strong",
        "confidence": 0.9,
    }
    assert Cluster(**base).verdict.value == "upheld"
    with pytest.raises(ValidationError):
        Cluster(**{**base, "member_claim_ids": ()})
    with pytest.raises(ValidationError):
        Cluster(**{**base, "cluster_id": "cluster-1"})
    with pytest.raises(ValidationError):
        Cluster(**{**base, "verdict": "winner"})


def test_artifact_is_inert_data_with_sorted_metadata():
    art = Artifact(kind="text", content="hello", metadata=[("b", "2"), ("a", "1")])
    assert art.metadata == (("a", "1"), ("b", "2"))
    assert not any(callable(v) for v in art.model_dump().values())
    with pytest.raises(ValidationError):
        Artifact(kind="text", content="x", run=lambda: None)


def test_schema_version_must_match():
    with pytest.raises(ValidationError):
        Frame(question_restated="q", problem_type="other", schema_version=2)


def test_models_round_trip_through_json():
    for obj in (perspective(), critique(), revision(), frame(), arbitration()):
        again = type(obj).model_validate_json(obj.model_dump_json())
        assert again == obj


def test_request_defaults_and_validation():
    req = CouncilRequest(request_id="r-1", question="Why?")
    assert req.profile_id == "general"
    assert req.depth.value == "standard"
    assert req.settings == CouncilSettings()
    with pytest.raises(ValidationError):
        CouncilRequest(request_id="r-1", question="")
    with pytest.raises(ValidationError):
        CouncilRequest(request_id="bad id", question="Why?")
    with pytest.raises(ValidationError):
        CouncilRequest(request_id="r", question="q", depth="deep")
    with pytest.raises(ValidationError):
        CouncilRequest(
            request_id="r",
            question="q",
            evidence=(EvidenceItem(id="e1", text="t"), EvidenceItem(id="e1", text="u")),
        )
    with pytest.raises(ValidationError):
        CouncilRequest(request_id="r", question="q", repository="/tmp/x")


def test_settings_are_data_only_and_bounded():
    with pytest.raises(ValidationError):
        CouncilSettings(wall_budget_s=0)
    with pytest.raises(ValidationError):
        CouncilSettings(max_concurrency=0)
    assert CouncilSettings(seed=7).seed == 7
    assert "provider" not in "".join(CouncilSettings.model_fields)
