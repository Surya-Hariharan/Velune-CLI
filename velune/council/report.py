"""Read the evidence an exploration run produced (a frame and perspectives) from its outcome.

A run over the exploration plan has no answer by design. Its evidence lives in the stage results:
the Moderator's frame, and one perspective per delivered seat. A seat that did not deliver has no
perspective here: absence is absence. The deterministic frame a failed Moderator is replaced by is
not stored on any result, so it is rebuilt from the request, exactly as the stage built it.
"""

from __future__ import annotations

from velune.council.contracts import Frame, Perspective
from velune.council.domain import StageId
from velune.council.drafts import fallback_frame
from velune.council.request import CouncilRequest
from velune.council.results import CouncilOutcome


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
