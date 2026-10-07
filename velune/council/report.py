"""Read the evidence an exploration or deliberation run produced from its outcome.

A run over a partial plan has no answer by design. Its evidence lives in the stage results: the
Moderator's frame, one perspective per delivered seat, the critiques the reviewers wrote, and the
revisions the seats made. A seat that did not deliver has nothing here: absence is absence. The
deterministic frame a failed Moderator is replaced by is not stored on any result, so it is rebuilt
from the request, exactly as the stage built it.

``final_positions`` is the one projection that reaches past a missing revision, and it says so: a
seat with no delivered revision is shown at its R1 perspective, marked unrevised, with the reason.
"""

from __future__ import annotations

from typing import Literal

from velune.council.contracts import Claim, Critique, Frame, Perspective, Revision, RevisionStatus
from velune.council.domain import StageId
from velune.council.drafts import fallback_frame
from velune.council.request import CouncilRequest
from velune.council.results import CouncilOutcome, StageStatus
from velune.council.serialization import Contract


def frame_of(outcome: CouncilOutcome, request: CouncilRequest) -> Frame | None:
    """The frame R1 worked from, or ``None`` if the frame stage never ran."""
    for stage in outcome.stage_results:
        if stage.stage is not StageId.FRAME:
            continue
        for result in stage.seat_results:
            if result.ok and isinstance(result.payload, Frame):
                return result.payload
        if stage.artifacts:  # an artifact without a delivered seat is the deterministic frame
            return fallback_frame(request.question)
    return None


def perspectives_of(outcome: CouncilOutcome) -> dict[str, Perspective]:
    """Perspectives by seat id, for the seats that delivered one, in the order they ran."""
    found: dict[str, Perspective] = {}
    for stage in outcome.stage_results:
        if stage.stage is not StageId.PERSPECTIVES:
            continue
        for result in stage.seat_results:
            if result.ok and isinstance(result.payload, Perspective):
                found[result.seat_id] = result.payload
    return found


def critiques_of(outcome: CouncilOutcome) -> dict[str, tuple[Critique, ...]]:
    """Critiques by target seat, from the reviewers that delivered, in reviewer order."""
    found: dict[str, list[Critique]] = {}
    for stage in outcome.stage_results:
        if stage.stage is not StageId.REVIEW:
            continue
        for result in stage.seat_results:
            if result.ok and isinstance(result.payload, tuple):
                for critique in result.payload:
                    if isinstance(critique, Critique):
                        found.setdefault(critique.target_seat, []).append(critique)
    return {target: tuple(items) for target, items in found.items()}


def revisions_of(outcome: CouncilOutcome) -> dict[str, Revision]:
    """Revisions by seat id, for the seats that delivered one."""
    found: dict[str, Revision] = {}
    for stage in outcome.stage_results:
        if stage.stage is not StageId.REVISION:
            continue
        for result in stage.seat_results:
            if result.ok and isinstance(result.payload, Revision):
                found[result.seat_id] = result.payload
    return found


class FinalPosition(Contract):
    """Where a seat stands after R3, and whether that is its revision or its unrevised R1 view."""

    seat_id: str
    source: Literal["revision", "perspective"]
    revised: bool
    position: str
    claims: tuple[Claim, ...]
    confidence: float
    # Why a seat is shown at its R1 perspective: "" when a revision exists, else one of
    # "revision_failed", "no_critiques" or "not_run".
    unrevised_reason: str = ""


def final_positions(outcome: CouncilOutcome) -> dict[str, FinalPosition]:
    """Each delivered seat's position after R3, never inventing a revision that did not happen."""
    perspectives = perspectives_of(outcome)
    revisions = revisions_of(outcome)
    revision_stage = next((s for s in outcome.stage_results if s.stage is StageId.REVISION), None)
    ran = revision_stage is not None and revision_stage.status is not StageStatus.SKIPPED
    attempted = {r.seat_id for r in revision_stage.seat_results} if revision_stage else set()
    found: dict[str, FinalPosition] = {}
    for seat, perspective in perspectives.items():
        revision = revisions.get(seat)
        if revision is not None:
            found[seat] = FinalPosition(
                seat_id=seat,
                source="revision",
                revised=revision.status is RevisionStatus.REVISED,
                position=revision.revised_position,
                claims=revision.claims,
                confidence=revision.revised_confidence,
            )
            continue
        if not ran:
            reason = "not_run"
        elif seat in attempted:
            reason = "revision_failed"
        else:
            reason = "no_critiques"
        found[seat] = FinalPosition(
            seat_id=seat,
            source="perspective",
            revised=False,
            position=perspective.position,
            claims=perspective.claims,
            confidence=perspective.confidence,
            unrevised_reason=reason,
        )
    return found
