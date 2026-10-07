"""R3: each seat revises its own perspective in light of the critiques addressed to it.

A seat is asked only if it delivered in R1 and at least one critique was addressed to it. Everything
it is shown comes through its own ``StageView``: its own R1 perspective (the contract grants no other)
and the critiques whose target is that seat (the contract grants no others). Other seats' perspectives
and revisions, critiques addressed to anyone else, and later stages are not inputs.

A seat that is asked and does not deliver (a failure, a timeout, a block, or a revision that stays
invalid after its one repair) is a typed absence with no revision. Nothing stands in for it: a reader
who wants the seat's final position falls back to its R1 perspective and says so (see ``report``).
A seat nobody reviewed is not asked and is noted as ``no_critiques``.
"""

from __future__ import annotations

from typing import Any

from velune.council.assembly import RevisionMaterial, build_revision_call
from velune.council.contracts import Critique, Perspective
from velune.council.domain import ArtifactKind, StageId
from velune.council.ports import PromptSource
from velune.council.profiles import SeatSpec
from velune.council.results import SeatResult
from velune.council.revisiondrafts import RevisionDraft, revision_from_draft
from velune.council.seatflow import SeatEvents, call_and_parse, note_fallback, run_seat_jobs
from velune.council.stages import STAGE_CONTRACTS, StageArtifact, StageOutput
from velune.council.state import StageContext


class RevisionStage:
    contract = STAGE_CONTRACTS[StageId.REVISION]

    def __init__(self, prompts: PromptSource) -> None:
        self._prompts = prompts

    def _gather(self, ctx: StageContext) -> tuple[set[str], dict[str, RevisionMaterial]]:
        """Who delivered in R1, and what each seat is shown, read through the seat's own view."""
        profile = ctx.profile
        order = tuple(spec.id for spec in profile.perspective_seats)
        delivered: set[str] = set()
        material: dict[str, RevisionMaterial] = {}
        for seat in profile.perspective_seats:
            view = ctx.view_for(seat.id)
            mine = view.read(ArtifactKind.PERSPECTIVE, seat=seat.id)
            if not mine or not isinstance(mine[0].payload, Perspective):
                continue
            delivered.add(seat.id)
            addressed = sorted(
                (
                    item
                    for item in view.read(ArtifactKind.CRITIQUE)
                    if isinstance(item.payload, Critique)
                ),
                key=lambda item: order.index(item.author),
            )
            if addressed:
                material[seat.id] = RevisionMaterial(
                    own=mine[0].payload,
                    critiques=tuple(
                        (profile.seat(item.author), item.payload) for item in addressed
                    ),
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
                call = build_revision_call(
                    view=view,
                    seat=seat,
                    material=gathered,
                    prompts=self._prompts,
                    timeout_s=ctx.settings.seat_timeout_s,
                )
                result = await call_and_parse(
                    invoker=ctx.invoker,
                    call=call,
                    draft_model=RevisionDraft,
                    convert=lambda draft: revision_from_draft(
                        draft,
                        seat=seat,
                        perspective=gathered.own,
                        critiques=gathered.reviewed,
                        profile=profile,
                    ),
                    emit=buffer,
                )
                note_fallback(result, buffer)
                return result

            return go

        results, notes = await run_seat_jobs(ctx, seats, job, on_error=events.record_error)
        events.flush(ctx, seats)
        artifacts = tuple(
            StageArtifact(kind=ArtifactKind.REVISION, author=result.seat_id, payload=result.payload)
            for result in results
            if result.ok
        )
        unreviewed = tuple(
            f"no_critiques:{spec.id}"
            for spec in profile.perspective_seats
            if spec.id in delivered and spec.id not in material
        )
        return StageOutput(
            seat_results=tuple(results),
            artifacts=artifacts,
            degradations=(*notes, *unreviewed),
            expected_seats=tuple(spec.id for spec in seats),
        )
