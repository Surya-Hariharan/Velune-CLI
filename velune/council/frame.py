"""R0: the Moderator scopes the question into a neutral ``Frame``.

The Moderator never answers. If it is absent for any reason (timeout, provider error, block, or a
reply that stays malformed after the one repair) the stage records the failed seat and supplies the
deterministic frame (the question itself, ``degraded=True``) so R1 can still run. That stand-in is
declared as a deterministic fallback, the only way the runner accepts an artifact without a
delivered seat result.

With a ``ContentScreen`` the Moderator's frame is also checked before it is committed. A refused
frame is treated like an absent Moderator (a ``blocked`` seat result with no payload, the
deterministic frame, a degraded run), so a hostile frame never reaches R1.
"""

from __future__ import annotations

from velune.council.assembly import build_frame_call, render_frame_context
from velune.council.domain import ArtifactKind, StageId
from velune.council.drafts import FrameDraft, fallback_frame, frame_from_draft
from velune.council.ports import ContentScreen, PromptSource
from velune.council.results import SeatResult, SeatStatus
from velune.council.seatflow import call_and_parse, note_fallback
from velune.council.stages import STAGE_CONTRACTS, StageArtifact, StageOutput
from velune.council.state import StageContext
from velune.council.trace import TraceEventKind


class FrameStage:
    contract = STAGE_CONTRACTS[StageId.FRAME]

    def __init__(self, prompts: PromptSource, screen: ContentScreen | None = None) -> None:
        self._prompts = prompts
        self._screen = screen

    async def run(self, ctx: StageContext) -> StageOutput:
        profile = ctx.profile
        seat = profile.moderator
        view = ctx.view_for(seat.id)
        call = build_frame_call(
            view=view,
            profile=profile,
            prompts=self._prompts,
            timeout_s=ctx.settings.seat_timeout_s,
        )
        language = view.requirements().language
        result = await call_and_parse(
            invoker=ctx.invoker,
            call=call,
            draft_model=FrameDraft,
            convert=lambda draft: frame_from_draft(
                draft, allowed_problem_types=profile.problem_types, language=language
            ),
            emit=ctx.emit,
        )
        note_fallback(result, ctx.emit)
        if result.ok and self._screen is not None:
            # The frame is model output that every R1 seat would be sent. If the screen refuses
            # it, the Moderator is treated as blocked and the hostile frame is never committed.
            if not self._screen.allows(render_frame_context(view, result.payload)):
                result = SeatResult.failure(
                    seat_id=result.seat_id,
                    kind=result.kind,
                    stage=result.stage,
                    status=SeatStatus.BLOCKED,
                    message="the frame was refused by the content screen",
                    model=result.model,
                    attempts=result.attempts,
                    fallback_used=result.fallback_used,
                    elapsed_ms=result.elapsed_ms,
                )
        if result.ok:
            payload = result.payload
            stand_in = False
        else:
            ctx.emit(TraceEventKind.FRAME_FALLBACK, seat=seat.id, status=result.status.value)
            payload = fallback_frame(view.question())
            stand_in = True
        return StageOutput(
            seat_results=(result,),
            artifacts=(StageArtifact(kind=ArtifactKind.FRAME, author=seat.id, payload=payload),),
            deterministic_fallback_used=stand_in,
        )
