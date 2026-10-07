"""Reasoning artifact shapes: claims, frame, perspectives, critiques, revisions, arbitration.

Structure only. These models validate types, caps, enums and id formats; they implement no
deliberation. Cross-artifact rules (claim ids exist, every contested cluster is addressed, evidence
over majority) belong to the ledger and arbitration phases and are deliberately absent.

There is no field for private reasoning, and unknown fields are rejected, so none can be added by
a model response either.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
)

from velune._compat import StrEnum
from velune.council.serialization import SCHEMA_VERSION, Contract, round_float

Confidence = Annotated[float, Field(ge=0.0, le=1.0), AfterValidator(round_float)]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,31}$")]
ClaimId = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2,3}-\d{1,3}$")]


Text80 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Text240 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
Text400 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]
Text600 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]
MaybeText240 = Annotated[str, StringConstraints(max_length=240)]


class ClaimStatus(StrEnum):
    KNOWN = "known"
    INFERRED = "inferred"
    ASSUMED = "assumed"
    UNCERTAIN = "uncertain"
    UNSUPPORTED = "unsupported"


class Severity(StrEnum):
    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"


class DisagreementKind(StrEnum):
    FACTUAL_ERROR = "factual_error"
    LOGICAL_GAP = "logical_gap"
    UNSUPPORTED = "unsupported"
    COUNTEREXAMPLE = "counterexample"
    MISSING_CONSIDERATION = "missing_consideration"
    SCOPE = "scope"
    ASSUMPTION = "assumption"


class RevisionStatus(StrEnum):
    REVISED = "revised"
    REAFFIRMED = "reaffirmed"
    CARRIED_FORWARD = "carried_forward"


class ChangeKind(StrEnum):
    RETRACTED = "retracted"
    WEAKENED = "weakened"
    STRENGTHENED = "strengthened"
    MODIFIED = "modified"
    ADDED = "added"


class ClusterVerdict(StrEnum):
    UPHELD = "upheld"
    REJECTED = "rejected"
    CONDITIONAL = "conditional"
    UNRESOLVED = "unresolved"


class EvidenceQuality(StrEnum):
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"
    NONE = "none"


class FactVerdict(StrEnum):
    SUPPORTED = "supported"
    PLAUSIBLE = "plausible"
    DISPUTED = "disputed"
    UNSUPPORTED = "unsupported"
    FALSE = "false"


class Claim(Contract):
    id: ClaimId
    text: Text240
    status: ClaimStatus
    support: Annotated[str, StringConstraints(max_length=200)] = ""
    confidence: Confidence
    depends_on: tuple[ClaimId, ...] = ()
    tags: tuple[Slug, ...] = ()


class Ambiguity(Contract):
    issue: Text240
    working_assumption: Text240


class Frame(Contract):
    schema_version: Literal[1] = SCHEMA_VERSION
    question_restated: Text600
    problem_type: Slug
    constraints: tuple[Text240, ...] = ()
    ambiguities: tuple[Ambiguity, ...] = ()
    dimensions: tuple[Text80, ...] = Field(default=(), max_length=6)
    missing_information: tuple[Text240, ...] = ()
    needs_clarification: bool = False
    clarification_question: MaybeText240 | None = None
    language: Annotated[str, StringConstraints(max_length=16)] = ""
    degraded: bool = False


class Alternative(Contract):
    option: Text240
    why_not_chosen: Text240


class Perspective(Contract):
    schema_version: Literal[1] = SCHEMA_VERSION
    seat_id: Slug
    position: Text400
    claims: tuple[Claim, ...] = Field(min_length=1, max_length=8)
    assumptions: tuple[Text240, ...] = ()
    uncertainties: tuple[Text240, ...] = ()
    alternatives: tuple[Alternative, ...] = ()
    confidence: Confidence
    rationale: Text600
    would_change_mind_if: tuple[Text240, ...] = ()
    frame_objections: tuple[Text240, ...] = ()

    @field_validator("claims")
    @classmethod
    def _unique_claim_ids(cls, claims: tuple[Claim, ...]) -> tuple[Claim, ...]:
        ids = [claim.id for claim in claims]
        if len(set(ids)) != len(ids):
            raise ValueError("claim ids must be unique within a perspective")
        return claims


class Disagreement(Contract):
    claim_id: ClaimId
    objection: Text240
    kind: DisagreementKind
    severity: Severity
    suggested_resolution: MaybeText240 = ""


class QuestionableAssumption(Contract):
    text: Text240
    why: Text240


class ClaimVerdictEntry(Contract):
    claim_id: ClaimId
    verdict: FactVerdict


class PeerInfluence(Contract):
    changed_own_view: bool = False
    affected_claim_ids: tuple[ClaimId, ...] = ()
    note: MaybeText240 = ""


class Critique(Contract):
    schema_version: Literal[1] = SCHEMA_VERSION
    reviewer_seat: Slug
    target_seat: Slug
    steelman: Text400
    agreements: tuple[ClaimId, ...] = ()
    disagreements: tuple[Disagreement, ...] = ()
    questionable_assumptions: tuple[QuestionableAssumption, ...] = ()
    unsupported_claims: tuple[ClaimId, ...] = ()
    missing_considerations: tuple[Text240, ...] = ()
    strong_arguments: tuple[ClaimId, ...] = ()
    counterexamples: tuple[Text240, ...] = ()
    claim_verdicts: tuple[ClaimVerdictEntry, ...] = ()
    peer_influence: PeerInfluence = PeerInfluence()
    no_material_issues: bool = False
    confidence: Confidence

    @field_validator("target_seat")
    @classmethod
    def _not_self(cls, value: str, info: ValidationInfo) -> str:
        if value == info.data.get("reviewer_seat"):
            raise ValueError("a seat cannot review itself")
        return value


class CauseRef(Contract):
    reviewer_seat: Slug
    objection_index: int = Field(ge=0)


class Change(Contract):
    claim_id: ClaimId
    change: ChangeKind
    reason: Text240
    caused_by: tuple[CauseRef, ...] = ()


class CritiqueResponse(Contract):
    critique: CauseRef
    note: Text240


class Revision(Contract):
    schema_version: Literal[1] = SCHEMA_VERSION
    seat_id: Slug
    status: RevisionStatus
    changes: tuple[Change, ...] = ()
    accepted: tuple[CritiqueResponse, ...] = ()
    rejected: tuple[CritiqueResponse, ...] = ()
    # Objections the seat could neither accept nor reject for lack of evidence.
    deferred: tuple[CritiqueResponse, ...] = ()
    revised_position: Text400
    claims: tuple[Claim, ...] = Field(min_length=1, max_length=8)
    revised_confidence: Confidence
    confidence_delta: Annotated[float, Field(ge=-1.0, le=1.0), AfterValidator(round_float)]
    remaining_disagreements: tuple[Text240, ...] = ()
    remaining_uncertainties: tuple[Text240, ...] = ()


class Cluster(Contract):
    cluster_id: Annotated[str, StringConstraints(pattern=r"^C\d{1,3}$")]
    canonical_text: Text240
    member_claim_ids: tuple[ClaimId, ...] = Field(min_length=1)
    supporting_seats: tuple[Slug, ...] = ()
    opposing_seats: tuple[Slug, ...] = ()
    verdict: ClusterVerdict
    decision_basis: Text240
    evidence_quality: EvidenceQuality
    confidence: Confidence
    majority_note: MaybeText240 | None = None


class Contradiction(Contract):
    between: tuple[ClaimId, ClaimId]
    description: Text240


class ConditionalConclusion(Contract):
    condition: Text240
    conclusion: Text240
    cluster_ids: tuple[str, ...] = ()


class Stance(Contract):
    seat_id: Slug
    stance: Text240


class UnresolvedConflict(Contract):
    issue: Text240
    positions: tuple[Stance, ...] = ()
    why_unresolved: Text240
    would_resolve_if: Text240


class RejectedClaim(Contract):
    claim_id: ClaimId
    reason: Text240


class GuidanceDirective(Contract):
    directive: Text240
    cluster_ids: tuple[str, ...] = ()


class ArbitrationResult(Contract):
    """What survived deliberation. Named for what it is; the contract carries no logic."""

    schema_version: Literal[1] = SCHEMA_VERSION
    clusters: tuple[Cluster, ...] = ()
    consensus_cluster_ids: tuple[str, ...] = ()
    contradictions: tuple[Contradiction, ...] = ()
    conditional_conclusions: tuple[ConditionalConclusion, ...] = ()
    unresolved_conflicts: tuple[UnresolvedConflict, ...] = ()
    rejected: tuple[RejectedClaim, ...] = ()
    overall_confidence: Confidence
    confidence_rationale: Text240
    must_preserve: tuple[Text240, ...] = ()
    synthesis_guidance: tuple[GuidanceDirective, ...] = ()
    flags: tuple[Slug, ...] = ()
    degraded: bool = False


class Artifact(Contract):
    """An inert output of a council run. It cannot execute, edit or request anything."""

    schema_version: Literal[1] = SCHEMA_VERSION
    kind: Slug
    media_type: Annotated[str, StringConstraints(min_length=3, max_length=80)] = "text/plain"
    content: str
    metadata: tuple[tuple[str, str], ...] = ()

    @field_validator("metadata")
    @classmethod
    def _sorted_metadata(cls, pairs: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(pairs))
