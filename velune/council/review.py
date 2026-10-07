"""R2: cross review. Each perspective seat critiques the peers its profile assigns it.

A reviewer is one call returning a review for every assigned target, so a reply is all or nothing
per reviewer. Everything a reviewer is shown comes through its own ``StageView``: its own R1
perspective, the peers the review graph names (in full), and, for a seat the graph audits by digest,
the claims of those peers. Whether a seat delivered in R1 is found the same way (it has its own
perspective in the store), so the stage never reads the whole panel. A seat with no perspective, or
with nobody left to review, is simply not asked: no one is rerouted and nothing stands in.

A reviewer that does not deliver contributes a typed failure and no critique. A target left with
fewer than two critiques is noted as ``under_reviewed``; it is never treated as having passed.

With a ``ContentScreen`` each reviewer's critiques are checked as R3 would show them, before they are
committed, so a hostile critique costs only its author's reviews.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from velune.council.assembly import (
    ReviewMaterial,
    build_review_call,
    render_critique_context,
)
from velune.council.contracts import Perspective
from velune.council.domain import ArtifactKind, StageId
from velune.council.ports import ContentScreen, PromptSource
from velune.council.profiles import SeatSpec
from velune.council.results import SeatResult
from velune.council.reviewdrafts import ReviewReplyDraft, critiques_from_draft
from velune.council.seatflow import SeatEvents, call_and_parse, note_fallback, refuse, run_seat_jobs
from velune.council.stages import STAGE_CONTRACTS, StageArtifact, StageOutput
from velune.council.state import StageContext

MIN_REVIEWS_PER_TARGET = 2


class ReviewStage:
    contract = STAGE_CONTRACTS[StageId.REVIEW]

    def __init__(self, prompts: PromptSource, screen: ContentScreen | None = None) -> None:
        self._prompts = prompts
        self._screen = screen

    def _gather(self, ctx: StageContext) -> tuple[set[str], dict[str, ReviewMaterial]]:
        """Who delivered in R1, and what each reviewer may be shown, read through its own view."""
        profile = ctx.profile
        order = tuple(spec.id for spec in profile.perspective_seats)
        delivered: set[str] = set()
        material: dict[str, ReviewMaterial] = {}
        for seat in profile.perspective_seats:
            view = ctx.view_for(seat.id)
            mine = view.read(ArtifactKind.PERSPECTIVE, seat=seat.id)
            if not mine or not isinstance(mine[0].payload, Perspective):
                continue
            delivered.add(seat.id)
            graph = profile.review_for(seat.id)
            if graph is None:
                continue
            full = tuple(
                (profile.seat(item.author), item.payload)
                for item in sorted(
                    view.read(ArtifactKind.PERSPECTIVE), key=lambda i: order.index(i.author)
                )
                if item.author != seat.id and isinstance(item.payload, Perspective)
            )
            digests = (
                tuple(
                    (profile.seat(d.author), d)
                    for d in sorted(
                        view.digest(ArtifactKind.PERSPECTIVE), key=lambda d: order.index(d.author)
                    )
                    if d.author != seat.id
                )
                if graph.digest
                else ()
            )
            if full or digests:
                material[seat.id] = ReviewMaterial(
                    own=mine[0].payload, order=order, full=full, digests=digests
                )
        return delivered, material

    async def run(self, ctx: StageContext) -> StageOutput:
        profile = ctx.profile
        delivered, material = self._gather(ctx)
        seats = tuple(spec for spec in profile.perspective_seats if spec.id in material)
        events = SeatEvents(seats)

        def job(seat: SeatSpec):
            buffer = events.emitter(seat)

            async def go() -> SeatResult[Any]:
                view = ctx.view_for(seat.id)
                gathered = material[seat.id]
                call = build_review_call(
                    view=view,
                    seat=seat,
                    material=gathered,
                    prompts=self._prompts,
                    timeout_s=ctx.settings.seat_timeout_s,
                )
                shown = gathered.shown_claim_ids()
                result = await call_and_parse(
                    invoker=ctx.invoker,
                    call=call,
                    draft_model=ReviewReplyDraft,
                    convert=lambda draft: critiques_from_draft(
                        draft, reviewer=seat, shown=shown, profile=profile
                    ),
                    emit=buffer,
                )
                note_fallback(result, buffer)
                if result.ok and self._screen is not None:
                    text = render_critique_context(view, [(seat, item) for item in result.payload])
                    if not self._screen.allows(text):
                        result = refuse(result, "the critiques were refused by the content screen")
                return result

            return go

        results, notes = await run_seat_jobs(ctx, seats, job, on_error=events.record_error)
        events.flush(ctx, seats)
        artifacts = tuple(
            StageArtifact(
                kind=ArtifactKind.CRITIQUE,
                author=result.seat_id,
                target=critique.target_seat,
                payload=critique,
            )
            for result in results
            if result.ok
            for critique in result.payload
        )
        received = Counter(item.target for item in artifacts)
        under = tuple(
            f"under_reviewed:{spec.id}"
            for spec in profile.perspective_seats
            if spec.id in delivered and received[spec.id] < MIN_REVIEWS_PER_TARGET
        )
        return StageOutput(
            seat_results=tuple(results),
            artifacts=artifacts,
            degradations=(*notes, *under),
            expected_seats=tuple(spec.id for spec in seats),
        )
