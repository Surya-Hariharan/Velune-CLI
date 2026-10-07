"""R1: each perspective seat forms its own view of the question, independently.

Independence is structural. Each seat's messages are built by ``build_perspective_call`` from that
seat's own ``StageView`` (which grants no peer artifact), the seat's own spec and static prompt
text; nothing a peer produced is an input. Results are committed by the runner only after the
stage ends, so concurrency cannot leak either.

A seat that did not deliver contributes a typed failure and no artifact. Nothing is substituted for
it. Trace events raised inside a seat's job are buffered and flushed in profile order so the trace
is the same however the seats were scheduled.
"""

from __future__ import annotations

from typing import Any

from velune.council.assembly import build_perspective_call
from velune.council.domain import ArtifactKind, StageId
from velune.council.drafts import PerspectiveDraft, perspective_from_draft
from velune.council.ports import PromptSource
from velune.council.profiles import SeatSpec
from velune.council.results import SeatResult
from velune.council.seatflow import call_and_parse, note_fallback, run_seat_jobs
from velune.council.stages import STAGE_CONTRACTS, StageArtifact, StageOutput
from velune.council.state import StageContext
from velune.council.trace import TraceEventKind


class PerspectiveStage:
    contract = STAGE_CONTRACTS[StageId.PERSPECTIVES]

    def __init__(self, prompts: PromptSource) -> None:
        self._prompts = prompts

    def _seats(self, ctx: StageContext) -> tuple[SeatSpec, ...]:
        return ctx.profile.perspective_seats

    async def run(self, ctx: StageContext) -> StageOutput:
        seats = self._seats(ctx)
        events: dict[str, list[tuple[TraceEventKind, dict[str, Any]]]] = {s.id: [] for s in seats}

        def job(seat: SeatSpec):
            def buffer(kind: TraceEventKind, **fields: Any) -> None:
                fields.pop("seat", None)  # the flush below attributes events to the seat
                events[seat.id].append((kind, fields))

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
                return result

            return go

        def bug(seat: SeatSpec, exc: Exception) -> None:
            # A bug inside a seat's job is a visible typed failure, never a swallowed success.
            events[seat.id].append((TraceEventKind.SEAT_ERROR, {"detail": type(exc).__name__}))

        # Finished seats are kept even if the stage deadline passes before the rest finish.
        results, notes = await run_seat_jobs(ctx, seats, job, on_error=bug)
        for seat in seats:  # profile order, whatever order the seats actually ran in
            for kind, fields in events[seat.id]:
                ctx.emit(kind, seat=seat.id, **fields)

        artifacts = tuple(
            StageArtifact(kind=ArtifactKind.PERSPECTIVE, author=r.seat_id, payload=r.payload)
            for r in results
            if r.ok
        )
        return StageOutput(seat_results=tuple(results), artifacts=artifacts, degradations=notes)
