"""The stage-agnostic sequencer.

``StagedCouncilRunner`` runs whatever stages it is given, in the order a depth plan names, and knows
nothing about what any stage does. Its job is the guarantees: a stage can fail the run but never
crash it, output that breaks its contract is rejected, a required failure skips what depends on it,
a cancelled run keeps the evidence it gathered, and every status is derived rather than asserted.

It never reads a clock or generates an id itself; ``Clock`` and ``IdSource`` are injected.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from velune.council.contracts import Artifact
from velune.council.domain import ArtifactKind, Criticality, StageId
from velune.council.ports import (
    AssignmentSource,
    AsyncioScheduler,
    Clock,
    IdSource,
    Scheduler,
    SeatInvoker,
    TraceSink,
)
from velune.council.profiles import ProfileRegistry, RoleProfile
from velune.council.request import CouncilRequest
from velune.council.results import (
    ArtifactRef,
    CouncilOutcome,
    OutcomeStatus,
    StageResult,
    StageStatus,
    derive_outcome_status,
    derive_stage_status,
)
from velune.council.stages import (
    STAGE_CONTRACTS,
    Stage,
    StageContract,
    StageOutput,
    StagePlan,
    VisibilityPolicy,
    validate_plan,
)
from velune.council.state import CouncilState, StageContext, StageView
from velune.council.trace import TraceEventKind, TraceLog


class CouncilCancelled(asyncio.CancelledError):
    """The run was cancelled. Carries the partial outcome so completed evidence is not lost."""

    def __init__(self, outcome: CouncilOutcome) -> None:
        super().__init__("council run cancelled")
        self.outcome = outcome


class StageContractViolation(Exception):
    """A stage returned output that breaks its own contract."""


class StagedCouncilRunner:
    def __init__(
        self,
        *,
        registry: ProfileRegistry,
        stages: Mapping[StageId, Stage] | Sequence[Stage],
        invoker: SeatInvoker,
        clock: Clock,
        ids: IdSource,
        scheduler: Scheduler | None = None,
        trace_sink: TraceSink | None = None,
        assignments: AssignmentSource | None = None,
        policy: VisibilityPolicy | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy or VisibilityPolicy(STAGE_CONTRACTS)
        self._invoker = invoker
        self._clock = clock
        self._ids = ids
        self._scheduler = scheduler or AsyncioScheduler()
        self._sink = trace_sink
        self._assignments = assignments
        self._stages = self._index(stages)

    def _index(self, stages: Mapping[StageId, Stage] | Sequence[Stage]) -> dict[StageId, Stage]:
        pairs = (
            list(stages.items())
            if isinstance(stages, Mapping)
            else [(s.contract.stage, s) for s in stages]
        )
        indexed: dict[StageId, Stage] = {}
        for stage_id, stage in pairs:
            if stage.contract.stage is not stage_id:
                raise ValueError(
                    f"stage registered as {stage_id.value} declares {stage.contract.stage.value}"
                )
            if stage.contract != self._policy.contract(stage_id):
                raise ValueError(f"stage {stage_id.value} does not match its declared contract")
            if stage_id in indexed:
                raise ValueError(f"stage {stage_id.value} registered twice")
            indexed[stage_id] = stage
        return indexed

    async def run(
        self, request: CouncilRequest, *, plan: Sequence[StageId] | None = None
    ) -> CouncilOutcome:
        """Run ``plan`` (default: the plan for ``request.depth``). Raises ``CouncilCancelled`` if cancelled.

        An unknown or non-runnable profile raises before anything starts: that is a caller error,
        not a run failure.
        """
        profile = self._registry.require_runnable(request.profile_id)
        trace = TraceLog(self._sink)
        state = CouncilState(
            request=request,
            profile=profile,
            run_id=self._ids.next_id("run"),
            trace=trace,
        )
        stages_planned = (
            validate_plan(plan) if plan is not None else StagePlan.for_depth(request.depth)
        )
        trace.emit(
            TraceEventKind.RUN_STARTED,
            detail=f"{state.run_id} plan={','.join(s.value for s in stages_planned)}",
        )
        started = self._clock.monotonic()
        current: StageId | None = None
        try:
            failed_stage: StageId | None = None
            for index, stage_id in enumerate(stages_planned):
                current = stage_id
                if failed_stage is not None:
                    reason = f"upstream_failed:{failed_stage.value}"
                    trace.emit(TraceEventKind.STAGE_SKIPPED, stage=stage_id, detail=reason)
                    state.stage_results.append(
                        StageResult(
                            stage=stage_id, status=StageStatus.SKIPPED, skipped_reason=reason
                        )
                    )
                    continue
                remaining = request.settings.wall_budget_s - (self._clock.monotonic() - started)
                result = await self._run_stage(state, stages_planned[index:], remaining)
                state.stage_results.append(result)
                if result.status is StageStatus.FAILED:
                    failed_stage = stage_id
            current = None
        except asyncio.CancelledError:
            raise CouncilCancelled(self._cancelled_outcome(state, current)) from None
        return self._finish(state)

    # ── one stage ────────────────────────────────────────────────────────────

    async def _run_stage(
        self, state: CouncilState, upcoming: Sequence[StageId], remaining_wall: float
    ) -> StageResult:
        stage_id = upcoming[0]
        contract = self._policy.contract(stage_id)
        trace = state.trace
        stage = self._stages.get(stage_id)
        if stage is None:
            trace.emit(TraceEventKind.STAGE_ERROR, stage=stage_id, detail="stage_not_registered")
            return self._unrunnable(contract, (f"stage_not_registered:{stage_id.value}",))
        if remaining_wall <= 0:
            trace.emit(TraceEventKind.STAGE_TIMEOUT, stage=stage_id, detail="wall_budget_exhausted")
            return self._unrunnable(contract, ("wall_budget_exhausted",))

        share_left = sum(self._policy.contract(s).budget_share for s in upcoming)
        allowance = remaining_wall * contract.budget_share / share_left
        trace.emit(TraceEventKind.STAGE_STARTED, stage=stage_id)
        ctx = self._context(state, contract, allowance)
        try:
            output = await asyncio.wait_for(stage.run(ctx), timeout=allowance)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            trace.emit(TraceEventKind.STAGE_TIMEOUT, stage=stage_id, detail=f"{allowance:.3f}s")
            return self._unrunnable(contract, ("stage_timeout",))
        except Exception as exc:
            trace.emit(TraceEventKind.STAGE_ERROR, stage=stage_id, detail=type(exc).__name__)
            return self._unrunnable(contract, (f"stage_error:{type(exc).__name__}",))

        try:
            self._validate(state.profile, contract, output)
        except StageContractViolation as exc:
            trace.emit(TraceEventKind.CONTRACT_VIOLATION, stage=stage_id, detail=str(exc))
            return self._unrunnable(contract, (f"contract_violation:{exc}",))
        return self._accept(state, contract, output)

    def _context(
        self, state: CouncilState, contract: StageContract, timeout_s: float
    ) -> StageContext:
        def view_for(seat_id: str) -> StageView:
            return StageView(
                policy=self._policy,
                stage=contract.stage,
                viewer=seat_id,
                request=state.request,
                store=state.store,
                assignments=self._assignments,
            )

        def emit(event: TraceEventKind, **kwargs: Any) -> None:
            state.trace.emit(event, stage=contract.stage, **kwargs)

        return StageContext(
            request=state.request,
            profile=state.profile,
            contract=contract,
            invoker=self._invoker,
            scheduler=self._scheduler,
            settings=state.request.settings,
            timeout_s=timeout_s,
            view_factory=view_for,
            emit_fn=emit,
        )

    def _accept(
        self, state: CouncilState, contract: StageContract, output: StageOutput
    ) -> StageResult:
        profile = state.profile
        expected = output.expected_seats
        if expected is None:
            expected = tuple(s.id for s in profile.all_seats if s.kind in contract.seat_kinds)
        quorum = contract.quorum
        if contract.quorum_from_profile:
            quorum = profile.default_quorum
        status, notes = derive_stage_status(
            expected_seats=expected,
            seat_results=output.seat_results,
            quorum=quorum,
            criticality=contract.criticality,
        )
        for seat_result in output.seat_results:
            state.trace.emit(
                TraceEventKind.SEAT_RESULT,
                stage=contract.stage,
                seat=seat_result.seat_id,
                status=seat_result.status.value,
            )
        state.commit(contract.stage, output)
        for item in output.artifacts:
            if item.kind is ArtifactKind.ARTIFACT and isinstance(item.payload, Artifact):
                state.artifacts.append(item.payload)
        if output.answer is not None:
            state.answer = output.answer
        if output.deterministic_fallback_used:
            state.deterministic_fallback_used = True
        degradations = tuple(dict.fromkeys((*output.degradations, *notes)))
        state.trace.emit(TraceEventKind.STAGE_FINISHED, stage=contract.stage, status=status.value)
        return StageResult(
            stage=contract.stage,
            status=status,
            seat_results=output.seat_results,
            artifacts=tuple(
                ArtifactRef(kind=a.kind, author=a.author, target=a.target) for a in output.artifacts
            ),
            degradations=degradations,
        )

    @staticmethod
    def _unrunnable(contract: StageContract, notes: tuple[str, ...]) -> StageResult:
        """A stage that could not produce usable output: failed if required, degraded if optional."""
        failed = contract.criticality is Criticality.REQUIRED
        return StageResult(
            stage=contract.stage,
            status=StageStatus.FAILED if failed else StageStatus.DEGRADED,
            degradations=notes,
        )

    @staticmethod
    def _validate(profile: RoleProfile, contract: StageContract, output: StageOutput) -> None:
        seats = {spec.id: spec for spec in profile.all_seats}
        if output.answer is not None and contract.writes is not ArtifactKind.ANSWER:
            raise StageContractViolation(f"{contract.stage.value} may not produce an answer")
        if output.expected_seats is not None:
            unknown = [s for s in output.expected_seats if s not in seats]
            if unknown:
                raise StageContractViolation(f"unknown expected seat {unknown[0]}")
        seen: set[str] = set()
        for result in output.seat_results:
            spec = seats.get(result.seat_id)
            if spec is None:
                raise StageContractViolation(f"unknown seat {result.seat_id}")
            if result.stage is not contract.stage:
                raise StageContractViolation(
                    f"seat result for {result.stage.value} returned from {contract.stage.value}"
                )
            if spec.kind is not result.kind or spec.kind not in contract.seat_kinds:
                raise StageContractViolation(
                    f"seat {result.seat_id} of kind {spec.kind.value} may not act in {contract.stage.value}"
                )
            if result.seat_id in seen:
                raise StageContractViolation(f"duplicate result for seat {result.seat_id}")
            seen.add(result.seat_id)
        delivered = {r.seat_id for r in output.seat_results if r.ok}
        for item in output.artifacts:
            if item.kind is not contract.writes and item.kind is not ArtifactKind.ARTIFACT:
                raise StageContractViolation(
                    f"{contract.stage.value} may not write {item.kind.value} artifacts"
                )
            spec = seats.get(item.author)
            if spec is None or spec.kind not in contract.seat_kinds:
                raise StageContractViolation(
                    f"{item.author} may not author in {contract.stage.value}"
                )
            if item.target is not None and item.target not in seats:
                raise StageContractViolation(f"unknown artifact target {item.target}")
            if item.author not in delivered and not output.deterministic_fallback_used:
                raise StageContractViolation(
                    f"artifact by {item.author} without a delivered seat result"
                )

    # ── outcomes ─────────────────────────────────────────────────────────────

    def _finish(self, state: CouncilState) -> CouncilOutcome:
        status = derive_outcome_status(
            state.stage_results, deterministic_fallback_used=state.deterministic_fallback_used
        )
        state.trace.emit(TraceEventKind.RUN_FINISHED, status=status.value)
        failure = None
        if status is OutcomeStatus.FAILED:
            first = next(r for r in state.stage_results if r.status is StageStatus.FAILED)
            failure = (
                f"{first.stage.value} failed: {', '.join(first.degradations) or 'no usable output'}"
            )
        return self._outcome(state, status, failure)

    def _cancelled_outcome(self, state: CouncilState, stage: StageId | None) -> CouncilOutcome:
        detail = stage.value if stage else "between stages"
        state.trace.emit(TraceEventKind.CANCELLED, stage=stage, detail=detail)
        return self._outcome(state, OutcomeStatus.CANCELLED, f"cancelled during {detail}")

    @staticmethod
    def _outcome(state: CouncilState, status: OutcomeStatus, failure: str | None) -> CouncilOutcome:
        succeeded = status in (OutcomeStatus.COMPLETED, OutcomeStatus.DEGRADED)
        notes: list[str] = []
        for result in state.stage_results:
            notes.extend(result.degradations)
        return CouncilOutcome(
            request_id=state.request.request_id,
            profile_id=state.profile.id,
            domain=state.profile.domain,
            status=status,
            answer=state.answer if succeeded else None,
            artifacts=tuple(state.artifacts) if succeeded else (),
            stage_results=tuple(state.stage_results),
            degradations=tuple(dict.fromkeys(notes)),
            failure_summary=None if succeeded else failure,
            trace_digest=state.trace.digest(),
        )
