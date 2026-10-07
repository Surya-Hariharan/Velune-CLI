"""What a seat is asked to return in R3, and the pure conversion into a ``Revision`` contract.

A revision is a patch on the seat's own R1 perspective, not a rewrite. The seat answers each major or
critical objection addressed to it (accept, partial, reject, or insufficient evidence) and lists the
smallest set of claim edits that follows from the objections it accepted. Claims it does not edit stay
exactly as they were.

The core owns identity and bookkeeping: the seat, the ids of new claims (the seat's prefix and the next
number above its highest R1 claim, never a reused one), every change kind, the confidence delta and the
status. A claim moves only through an edit that cites an objection the seat accepted, so nothing
changes without a cause and a rejected critique cannot justify a change. Failures here are
``ValueError`` and earn the one repair; a revision that stays invalid is an absence.

Drafts are as strict as the contracts: frozen, unknown fields rejected, no field for private reasoning.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import Field

from velune._compat import StrEnum
from velune.council.contracts import (
    CauseRef,
    Change,
    ChangeKind,
    Claim,
    ClaimId,
    ClaimStatus,
    Confidence,
    Critique,
    CritiqueResponse,
    Disagreement,
    Perspective,
    Revision,
    RevisionStatus,
    Severity,
    Slug,
    Text240,
    Text400,
)
from velune.council.profiles import RoleProfile, SeatSpec
from velune.council.reviewdrafts import MAX_OBJECTIONS, foreign_claim_ids, profile_prefixes
from velune.council.serialization import Contract, round_float

MAX_EDITS = 8
MAX_RESPONSES = 30
MAX_LISTED = 6
MAX_CLAIMS = 8
MAX_CLAIM_NUMBER = 999


class Decision(StrEnum):
    ACCEPT = "accept"
    PARTIAL = "partial"
    REJECT = "reject"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class EditOp(StrEnum):
    MODIFY = "modify"
    RETRACT = "retract"
    ADD = "add"


class CauseDraft(Contract):
    reviewer: Slug
    objection: int = Field(ge=1, le=MAX_OBJECTIONS)  # 1-based, as the revision request numbers them


class ResponseDraft(Contract):
    reviewer: Slug
    objection: int = Field(ge=1, le=MAX_OBJECTIONS)
    decision: Decision
    note: Text240


class EditDraft(Contract):
    """One change to one claim. Unlisted claims stay exactly as they were."""

    op: EditOp
    claim_id: ClaimId | None = None  # an existing claim for modify/retract; absent for add
    text: Text240 | None = None
    status: ClaimStatus | None = None
    support: str | None = Field(default=None, max_length=200)
    confidence: Confidence | None = None
    depends_on: tuple[ClaimId, ...] | None = Field(default=None, max_length=MAX_CLAIMS)
    tags: tuple[Slug, ...] | None = None
    reason: Text240
    caused_by: tuple[CauseDraft, ...] = Field(min_length=1, max_length=4)


class RevisionDraft(Contract):
    responses: tuple[ResponseDraft, ...] = Field(default=(), max_length=MAX_RESPONSES)
    edits: tuple[EditDraft, ...] = Field(default=(), max_length=MAX_EDITS)
    position: Text400 | None = None  # only if the position itself changes
    confidence: Confidence | None = None  # only if overall confidence changes
    remaining_disagreements: tuple[Text240, ...] = Field(default=(), max_length=MAX_LISTED)
    remaining_uncertainties: tuple[Text240, ...] = Field(default=(), max_length=MAX_LISTED)


Key = tuple[str, int]
Objections = dict[Key, Disagreement]
_CONTENT_FIELDS = ("text", "status", "support", "confidence", "depends_on", "tags")


def objections_of(critiques: Sequence[tuple[str, Critique]]) -> Objections:
    """Every objection addressed to a seat, keyed by (reviewer, 1-based number)."""
    found: Objections = {}
    for reviewer, critique in critiques:
        for number, item in enumerate(critique.disagreements, start=1):
            found[(reviewer, number)] = item
    return found


def required_responses(critiques: Sequence[tuple[str, Critique]]) -> list[Key]:
    """The objections a revision must answer: every major or critical one, in reading order."""
    return [
        key
        for key, item in objections_of(critiques).items()
        if item.severity in (Severity.MAJOR, Severity.CRITICAL)
    ]


def _key(item: CauseDraft | ResponseDraft) -> Key:
    return (item.reviewer, item.objection)


def _next_claim_number(perspective: Perspective) -> int:
    return max(int(claim.id.rsplit("-", 1)[1]) for claim in perspective.claims) + 1


def _change_kind(old: Claim, new: Claim) -> ChangeKind:
    only_confidence = old.model_copy(update={"confidence": new.confidence}) == new
    if only_confidence and new.confidence < old.confidence:
        return ChangeKind.WEAKENED
    if only_confidence:
        return ChangeKind.STRENGTHENED
    return ChangeKind.MODIFIED


def _answers(
    draft: RevisionDraft, objections: Objections
) -> tuple[dict[Key, Decision], dict[Key, str]]:
    decisions: dict[Key, Decision] = {}
    notes: dict[Key, str] = {}
    for index, response in enumerate(draft.responses):
        key = _key(response)
        if key not in objections:
            raise ValueError(
                f"responses.{index}: there is no objection {response.objection} from {response.reviewer}"
            )
        if key in decisions:
            raise ValueError(f"responses.{index}: objection {key[1]} from {key[0]} answered twice")
        decisions[key] = response.decision
        notes[key] = response.note
    return decisions, notes


def revision_from_draft(
    draft: RevisionDraft,
    *,
    seat: SeatSpec,
    perspective: Perspective,
    critiques: Sequence[tuple[str, Critique]],
    profile: RoleProfile,
) -> Revision:
    """Apply a revision draft to the seat's own R1 perspective, or raise ``ValueError``."""
    objections = objections_of(critiques)
    reviewers = [reviewer for reviewer, _ in critiques]
    decisions, notes = _answers(draft, objections)
    unanswered = [key for key in required_responses(critiques) if key not in decisions]
    if unanswered:
        reviewer, number = unanswered[0]
        raise ValueError(
            "responses: every major or critical objection needs a response "
            f"(missing: {reviewer} #{number})"
        )

    claims: dict[str, Claim] = {claim.id: claim for claim in perspective.claims}
    original = dict(claims)
    touched: set[str] = set()
    additions: list[Claim] = []
    changes: list[Change] = []
    next_number = _next_claim_number(perspective)
    for index, edit in enumerate(draft.edits):
        where = f"edits.{index}"
        for cause in edit.caused_by:
            if _key(cause) not in objections:
                raise ValueError(
                    f"{where}: there is no objection {cause.objection} from {cause.reviewer}"
                )
            if decisions.get(_key(cause)) not in (Decision.ACCEPT, Decision.PARTIAL):
                raise ValueError(
                    f"{where}: a change must cite an objection you accepted "
                    f"({cause.reviewer} #{cause.objection})"
                )
        caused_by = tuple(
            CauseRef(reviewer_seat=cause.reviewer, objection_index=cause.objection - 1)
            for cause in edit.caused_by
        )
        provided = {
            name: getattr(edit, name) for name in _CONTENT_FIELDS if getattr(edit, name) is not None
        }
        if edit.op is EditOp.ADD:
            if edit.claim_id is not None:
                raise ValueError(f"{where}: a new claim takes no id; the system numbers it")
            missing = [name for name in ("text", "status", "confidence") if name not in provided]
            if missing:
                raise ValueError(f"{where}: a new claim needs {', '.join(missing)}")
            if next_number > MAX_CLAIM_NUMBER:
                raise ValueError(f"{where}: no claim numbers are left")
            added = Claim(id=f"{seat.claim_prefix}-{next_number}", **provided)
            next_number += 1
            additions.append(added)
            changes.append(
                Change(
                    claim_id=added.id,
                    change=ChangeKind.ADDED,
                    reason=edit.reason,
                    caused_by=caused_by,
                )
            )
            continue
        if edit.claim_id is None or edit.claim_id not in original:
            raise ValueError(f"{where}: {edit.op.value} needs the id of one of your own claims")
        if edit.claim_id in touched:
            raise ValueError(f"{where}: {edit.claim_id} is edited more than once")
        touched.add(edit.claim_id)
        if edit.op is EditOp.RETRACT:
            if provided:
                raise ValueError(f"{where}: a retraction carries no new content")
            del claims[edit.claim_id]
            changes.append(
                Change(
                    claim_id=edit.claim_id,
                    change=ChangeKind.RETRACTED,
                    reason=edit.reason,
                    caused_by=caused_by,
                )
            )
            continue
        if not provided:
            raise ValueError(f"{where}: a modification must change something")
        old = original[edit.claim_id]
        new = Claim(**{**old.model_dump(), **provided})
        if new == old:
            raise ValueError(f"{where}: {edit.claim_id} is unchanged by this edit")
        claims[edit.claim_id] = new
        changes.append(
            Change(
                claim_id=edit.claim_id,
                change=_change_kind(old, new),
                reason=edit.reason,
                caused_by=caused_by,
            )
        )

    final = [*claims.values(), *additions]
    if not 1 <= len(final) <= MAX_CLAIMS:
        raise ValueError(f"edits: a revision must leave between 1 and {MAX_CLAIMS} claims")
    final_ids = {claim.id for claim in final}
    for claim in final:
        for dep in claim.depends_on:
            if dep == claim.id or dep not in final_ids:
                raise ValueError(
                    f"edits: {claim.id} depends on {dep}, which is not one of your claims"
                )

    new_position = draft.position if draft.position is not None else perspective.position
    new_confidence = draft.confidence if draft.confidence is not None else perspective.confidence
    position_changed = new_position != perspective.position
    confidence_changed = new_confidence != perspective.confidence
    accepted_any = any(d in (Decision.ACCEPT, Decision.PARTIAL) for d in decisions.values())
    if (position_changed or confidence_changed) and not accepted_any:
        raise ValueError("position: a change of position or confidence needs an accepted objection")

    allowed = {seat.claim_prefix, *(profile.seat(r).claim_prefix for r in reviewers)}
    texts = [
        new_position,
        *draft.remaining_disagreements,
        *draft.remaining_uncertainties,
        *notes.values(),
        *(edit.reason for edit in draft.edits),
        *(claim.text for claim in final),
    ]
    stray = foreign_claim_ids(texts, allowed=allowed, known=profile_prefixes(profile))
    if stray:
        raise ValueError(f"text: cites {stray[0]}, which is not one of the claims you were shown")

    def responses_for(*wanted: Decision) -> tuple[CritiqueResponse, ...]:
        return tuple(
            CritiqueResponse(
                critique=CauseRef(reviewer_seat=key[0], objection_index=key[1] - 1),
                note=notes[key],
            )
            for key in sorted(decisions, key=lambda k: (reviewers.index(k[0]), k[1]))
            if decisions[key] in wanted
        )

    unchanged = not draft.edits and not position_changed and not confidence_changed
    return Revision(
        seat_id=seat.id,
        status=RevisionStatus.REAFFIRMED if unchanged else RevisionStatus.REVISED,
        changes=tuple(changes),
        accepted=responses_for(Decision.ACCEPT, Decision.PARTIAL),
        rejected=responses_for(Decision.REJECT),
        deferred=responses_for(Decision.INSUFFICIENT_EVIDENCE),
        revised_position=new_position,
        claims=tuple(final),
        revised_confidence=new_confidence,
        confidence_delta=round_float(new_confidence - perspective.confidence),
        remaining_disagreements=draft.remaining_disagreements,
        remaining_uncertainties=draft.remaining_uncertainties,
    )
