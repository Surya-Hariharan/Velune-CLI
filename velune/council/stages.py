"""Stage contracts R0-R5 as data, the depth plans, and the visibility policy derived from them.

No stage is implemented here. A contract says which seats may act, what they may read, what they
write and what happens when they cannot; the runner and ``StageView`` enforce it. Independence is a
contract rather than a convention: R1 declares no perspective reads at all.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import Field, model_validator

from velune.council.contracts import Slug
from velune.council.domain import (
    STAGE_ORDER,
    ArtifactKind,
    Criticality,
    Depth,
    FallbackPolicy,
    QuorumRule,
    ReadScope,
    SeatKind,
    StageId,
)
from velune.council.results import SeatResult
from velune.council.serialization import Contract

if TYPE_CHECKING:
    from velune.council.state import StageContext

__all__ = [
    "DELIBERATION_PLAN",
    "EXPLORATION_PLAN",
    "STAGE_CONTRACTS",
    "QuorumRule",
    "ReadRule",
    "Stage",
    "StageArtifact",
    "StageContract",
    "StageOutput",
    "StagePlan",
    "VisibilityPolicy",
    "validate_plan",
]


class ReadRule(Contract):
    kind: ArtifactKind
    scope: ReadScope


class StageContract(Contract):
    stage: StageId
    seat_kinds: tuple[SeatKind, ...] = Field(min_length=1)
    reads: tuple[ReadRule, ...]
    writes: ArtifactKind
    parallel: bool
    criticality: Criticality
    quorum: QuorumRule | None = None
    quorum_from_profile: bool = False
    fallback: FallbackPolicy = FallbackPolicy.NONE
    budget_share: float = Field(gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _one_quorum_source(self) -> StageContract:
        if self.quorum is not None and self.quorum_from_profile:
            raise ValueError("a stage takes its quorum from the contract or the profile, not both")
        return self


class StageArtifact(Contract):
    """An artifact a stage hands back. It becomes visible to later stages only."""

    kind: ArtifactKind
    author: Slug
    target: Slug | None = None
    payload: Any


class StageOutput(Contract):
    seat_results: tuple[SeatResult[Any], ...] = ()
    artifacts: tuple[StageArtifact, ...] = ()
    degradations: tuple[str, ...] = ()
    answer: str | None = None
    # Seats the stage meant to hear from. ``None`` means every profile seat of an allowed kind.
    expected_seats: tuple[Slug, ...] | None = None
    deterministic_fallback_used: bool = False


@runtime_checkable
class Stage(Protocol):
    contract: StageContract

    async def run(self, ctx: StageContext) -> StageOutput: ...


def _rules(*pairs: tuple[ArtifactKind, ReadScope]) -> tuple[ReadRule, ...]:
    return tuple(ReadRule(kind=kind, scope=scope) for kind, scope in pairs)


K, S = ArtifactKind, ReadScope

STAGE_CONTRACTS: Mapping[StageId, StageContract] = {
    StageId.FRAME: StageContract(
        stage=StageId.FRAME,
        seat_kinds=(SeatKind.MODERATOR,),
        reads=_rules((K.QUESTION, S.ALL)),
        writes=K.FRAME,
        parallel=False,
        criticality=Criticality.OPTIONAL,
        fallback=FallbackPolicy.DETERMINISTIC_DEFAULT,
        budget_share=0.05,
    ),
    StageId.PERSPECTIVES: StageContract(
        stage=StageId.PERSPECTIVES,
        seat_kinds=(SeatKind.PERSPECTIVE,),
        reads=_rules(
            (K.QUESTION, S.ALL),
            (K.FRAME, S.ALL),
            (K.EVIDENCE, S.ALL),
            (K.PERSPECTIVE, S.NONE),
        ),
        writes=K.PERSPECTIVE,
        parallel=True,
        criticality=Criticality.REQUIRED,
        quorum_from_profile=True,
        budget_share=0.25,
    ),
    StageId.REVIEW: StageContract(
        stage=StageId.REVIEW,
        seat_kinds=(SeatKind.PERSPECTIVE,),
        reads=_rules(
            (K.QUESTION, S.ALL),
            (K.FRAME, S.ALL),
            (K.EVIDENCE, S.ALL),
            (K.PERSPECTIVE, S.OWN),
            (K.PERSPECTIVE, S.ASSIGNED),
            (K.PERSPECTIVE, S.DIGEST_ALL),
        ),
        writes=K.CRITIQUE,
        parallel=True,
        criticality=Criticality.OPTIONAL,
        fallback=FallbackPolicy.SKIP,
        budget_share=0.20,
    ),
    StageId.REVISION: StageContract(
        stage=StageId.REVISION,
        seat_kinds=(SeatKind.PERSPECTIVE,),
        reads=_rules(
            (K.QUESTION, S.ALL),
            (K.FRAME, S.ALL),
            (K.EVIDENCE, S.ALL),
            (K.PERSPECTIVE, S.OWN),
            (K.CRITIQUE, S.ADDRESSED_TO_SELF),
        ),
        writes=K.REVISION,
        parallel=True,
        criticality=Criticality.OPTIONAL,
        fallback=FallbackPolicy.DETERMINISTIC_DEFAULT,
        budget_share=0.20,
    ),
    StageId.ARBITRATION: StageContract(
        stage=StageId.ARBITRATION,
        seat_kinds=(SeatKind.ARBITRATOR,),
        reads=_rules(
            (K.QUESTION, S.ALL),
            (K.FRAME, S.ALL),
            (K.EVIDENCE, S.ALL),
            (K.PERSPECTIVE, S.ALL),
            (K.CRITIQUE, S.ALL),
            (K.REVISION, S.ALL),
        ),
        writes=K.DECISION,
        parallel=False,
        criticality=Criticality.REQUIRED,
        fallback=FallbackPolicy.DETERMINISTIC_DEFAULT,
        budget_share=0.15,
    ),
    StageId.SYNTHESIS: StageContract(
        stage=StageId.SYNTHESIS,
        seat_kinds=(SeatKind.SYNTHESIZER,),
        reads=_rules(
            (K.QUESTION, S.ALL),
            (K.FRAME, S.ALL),
            (K.DECISION, S.ALL),
            (K.PERSPECTIVE, S.DIGEST_ALL),
            (K.REVISION, S.DIGEST_ALL),
        ),
        writes=K.ANSWER,
        parallel=False,
        criticality=Criticality.REQUIRED,
        fallback=FallbackPolicy.DETERMINISTIC_DEFAULT,
        budget_share=0.15,
    ),
}


class StagePlan:
    """Which stages a depth runs, in order. The runner executes whatever plan it is given."""

    _SKIPPED_WHEN_QUICK = frozenset({StageId.REVIEW, StageId.REVISION})

    @classmethod
    def for_depth(cls, depth: Depth) -> tuple[StageId, ...]:
        if depth is Depth.QUICK:
            return tuple(s for s in STAGE_ORDER if s not in cls._SKIPPED_WHEN_QUICK)
        return STAGE_ORDER


# The Phase 2A partial plan: frame and perspectives only. It ends before arbitration and synthesis,
# so a run over it produces evidence (a frame and perspectives) and never an answer.
EXPLORATION_PLAN: tuple[StageId, ...] = (StageId.FRAME, StageId.PERSPECTIVES)

# The Phase 2B partial plan: the exploration plan plus cross review and revision. It still ends before
# arbitration and synthesis, so a run over it yields a validated record of what each seat first said,
# what its reviewers objected to and how it revised, and never an answer.
DELIBERATION_PLAN: tuple[StageId, ...] = (
    StageId.FRAME,
    StageId.PERSPECTIVES,
    StageId.REVIEW,
    StageId.REVISION,
)


def validate_plan(plan: Sequence[StageId]) -> tuple[StageId, ...]:
    """A plan must be non-empty and an ordered subsequence of the protocol stages."""
    stages = tuple(plan)
    if not stages:
        raise ValueError("a stage plan cannot be empty")
    positions = [STAGE_ORDER.index(stage) for stage in stages]
    if positions != sorted(set(positions)):
        raise ValueError("a stage plan must list protocol stages in order, without repeats")
    return stages


class VisibilityPolicy:
    """What each stage may read, derived from the stage contracts.

    This is the single source of truth ``StageView`` consults, so the independence guarantee is a
    property of the data and can be asserted against it.
    """

    def __init__(self, contracts: Mapping[StageId, StageContract] | None = None) -> None:
        self._contracts = contracts if contracts is not None else STAGE_CONTRACTS

    def contract(self, stage: StageId) -> StageContract:
        return self._contracts[stage]

    def scopes(self, stage: StageId, kind: ArtifactKind) -> tuple[ReadScope, ...]:
        """Granted scopes for ``kind`` in ``stage``; empty when the stage may not read it."""
        granted = tuple(rule.scope for rule in self._contracts[stage].reads if rule.kind is kind)
        return tuple(scope for scope in granted if scope is not ReadScope.NONE)

    def can_read(self, stage: StageId, kind: ArtifactKind) -> bool:
        return bool(self.scopes(stage, kind))

    def matrix(self) -> dict[StageId, dict[ArtifactKind, tuple[ReadScope, ...]]]:
        """The full visibility table: stage -> kind -> granted scopes (kinds with no access omitted)."""
        table: dict[StageId, dict[ArtifactKind, tuple[ReadScope, ...]]] = {}
        for stage in self._contracts:
            row = {kind: self.scopes(stage, kind) for kind in ArtifactKind}
            table[stage] = {kind: scopes for kind, scopes in row.items() if scopes}
        return table
