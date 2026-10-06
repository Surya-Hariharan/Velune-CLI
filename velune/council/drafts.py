"""What a model is asked to return, and the pure conversion into the strict contracts.

A *draft* is the wire format: the smallest shape a model must produce. The core, not the model,
assigns identity: the seat id comes from the seat, claim ids are ``<prefix>-<n>`` by position, and
fields such as ``degraded`` and ``language`` are never the model's to set. A model therefore cannot
impersonate another seat or break the id format, and ids are deterministic across runs.

Drafts are as strict as the contracts: frozen, unknown fields rejected, so a ``reasoning`` or
``thoughts`` field (or a ``schema_version``) in a reply is a validation error.
"""

from __future__ import annotations

from collections.abc import Collection

from pydantic import Field

from velune.council.contracts import (
    Alternative,
    Ambiguity,
    Claim,
    ClaimStatus,
    Confidence,
    Frame,
    MaybeText240,
    Perspective,
    Slug,
    Text80,
    Text240,
    Text400,
    Text600,
)
from velune.council.profiles import SeatSpec
from velune.council.serialization import Contract

MAX_CLAIMS = 8
MAX_DIMENSIONS = 6


class FrameDraft(Contract):
    question_restated: Text600
    problem_type: Slug
    constraints: tuple[Text240, ...] = ()
    ambiguities: tuple[Ambiguity, ...] = ()
    dimensions: tuple[Text80, ...] = Field(default=(), max_length=MAX_DIMENSIONS)
    missing_information: tuple[Text240, ...] = ()
    needs_clarification: bool = False
    clarification_question: MaybeText240 | None = None


class ClaimDraft(Contract):
    text: Text240
    status: ClaimStatus
    support: str = Field(default="", max_length=200)
    confidence: Confidence
    # 1-based numbers of *earlier* claims in the same list that this claim rests on.
    depends_on: tuple[int, ...] = ()
    tags: tuple[Slug, ...] = ()


class PerspectiveDraft(Contract):
    position: Text400
    claims: tuple[ClaimDraft, ...] = Field(min_length=1, max_length=MAX_CLAIMS)
    assumptions: tuple[Text240, ...] = ()
    uncertainties: tuple[Text240, ...] = ()
    alternatives: tuple[Alternative, ...] = ()
    confidence: Confidence
    rationale: Text600
    would_change_mind_if: tuple[Text240, ...] = ()
    frame_objections: tuple[Text240, ...] = ()


def frame_from_draft(
    draft: FrameDraft, *, allowed_problem_types: Collection[str], language: str = ""
) -> Frame:
    """Build the contract ``Frame``; ``problem_type`` must come from the profile's vocabulary."""
    if draft.problem_type not in allowed_problem_types:
        raise ValueError(f"problem_type: {draft.problem_type!r} is not an allowed value")
    return Frame(
        question_restated=draft.question_restated,
        problem_type=draft.problem_type,
        constraints=draft.constraints,
        ambiguities=draft.ambiguities,
        dimensions=draft.dimensions,
        missing_information=draft.missing_information,
        needs_clarification=draft.needs_clarification,
        clarification_question=draft.clarification_question,
        language=language[:16],
        degraded=False,
    )


def perspective_from_draft(draft: PerspectiveDraft, *, seat: SeatSpec) -> Perspective:
    """Build the contract ``Perspective``, assigning the seat id and claim ids."""
    claims: list[Claim] = []
    for number, item in enumerate(draft.claims, start=1):
        for dep in item.depends_on:
            if not 1 <= dep < number:
                raise ValueError(
                    f"claims.{number - 1}.depends_on: {dep} must number an earlier claim"
                )
        claims.append(
            Claim(
                id=f"{seat.claim_prefix}-{number}",
                text=item.text,
                status=item.status,
                support=item.support,
                confidence=item.confidence,
                depends_on=tuple(f"{seat.claim_prefix}-{dep}" for dep in item.depends_on),
                tags=item.tags,
            )
        )
    return Perspective(
        seat_id=seat.id,
        position=draft.position,
        claims=tuple(claims),
        assumptions=draft.assumptions,
        uncertainties=draft.uncertainties,
        alternatives=draft.alternatives,
        confidence=draft.confidence,
        rationale=draft.rationale,
        would_change_mind_if=draft.would_change_mind_if,
        frame_objections=draft.frame_objections,
    )


def fallback_frame(question: str) -> Frame:
    """The deterministic frame used when the Moderator is absent: the question, nothing more."""
    return Frame(
        question_restated=question.strip()[:600] or "(no question)",
        problem_type="other",
        degraded=True,
    )
