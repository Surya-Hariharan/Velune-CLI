"""R1: each perspective seat forms its own view of the question, independently.

Independence is structural. Each seat's messages are built by ``build_perspective_call`` from that
seat's own ``StageView`` (which grants no peer artifact), the seat's own spec and static prompt
text; nothing a peer produced is an input. Results are committed by the runner only after the
stage ends, so concurrency cannot leak either.

A seat that did not deliver contributes a typed failure and no artifact. Nothing is substituted for
it. Trace events raised inside a seat's job are buffered and flushed in profile order so the trace
is the same however the seats were scheduled.

With a ``ContentScreen`` each delivered perspective is also checked as R2 would render it for a
reviewer, before it is committed. A refused perspective is a ``blocked`` absence, so one hostile
perspective cannot block the reviewers who would have read it.
"""

from __future__ import annotations

from typing import Any

from velune.council.assembly import build_perspective_call, render_perspective_context
from velune.council.domain import ArtifactKind, StageId
from velune.council.drafts import PerspectiveDraft, perspective_from_draft
from velune.council.ports import ContentScreen, PromptSource
from velune.council.profiles import SeatSpec
from velune.council.results import SeatResult
from velune.council.seatflow import SeatEvents, call_and_parse, note_fallback, refuse, run_seat_jobs
from velune.council.stages import STAGE_CONTRACTS, StageArtifact, StageOutput
from velune.council.state import StageContext


class PerspectiveStage:
    contract = STAGE_CONTRACTS[StageId.PERSPECTIVES]

    def __init__(self, prompts: PromptSource, screen: ContentScreen | None = None) -> None:
        self._prompts = prompts
        self._screen = screen

    def _seats(self, ctx: StageContext) -> tuple[SeatSpec, ...]:
        return ctx.profile.perspective_seats

    async def run(self, ctx: StageContext) -> StageOutput:
        seats = self._seats(ctx)
        events = SeatEvents(seats)

        def job(seat: SeatSpec):
            buffer = events.emitter(seat)

            async def go() -> SeatResult[Any]:
                view = ctx.view_for(seat.id)
                call = build_perspective_call(
                    view=view,
                    seat=seat,
                    prompts=self._prompts,
                    timeout_s=ctx.settings.seat_timeout_s,
                )
                result = await call_and_parse(
                    invoker=ctx.invoker,
                    call=call,
                    draft_model=PerspectiveDraft,
                    convert=lambda draft: perspective_from_draft(draft, seat=seat),
                    emit=buffer,
                )
                note_fallback(result, buffer)
                if result.ok and self._screen is not None:
                    text = render_perspective_context(view, seat, result.payload)
                    if not self._screen.allows(text):
                        result = refuse(result, "the perspective was refused by the content screen")
                return result

            return go

        # Finished seats are kept even if the stage deadline passes before the rest finish.
        results, notes = await run_seat_jobs(ctx, seats, job, on_error=events.record_error)
        events.flush(ctx, seats)
        artifacts = tuple(
            StageArtifact(kind=ArtifactKind.PERSPECTIVE, author=r.seat_id, payload=r.payload)
            for r in results
            if r.ok
        )
        return StageOutput(seat_results=tuple(results), artifacts=artifacts, degradations=notes)
