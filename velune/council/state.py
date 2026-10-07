"""Per-run state and the only way a stage may read it.

Stages never touch ``CouncilState`` or the ``ArtifactStore``. They get a ``StageView`` bound to one
seat, and every read goes through ``VisibilityPolicy``; anything the stage's contract does not grant
raises ``VisibilityViolation``. Artifacts a stage writes are committed by the runner only after the
stage finishes, so seats running side by side inside one stage can never see each other either.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from velune.council.contracts import Artifact, Claim
from velune.council.domain import ArtifactKind, ReadScope, StageId
from velune.council.ports import (
    AssignmentSource,
    DigestAssignmentSource,
    Scheduler,
    SeatInvoker,
)
from velune.council.profiles import RoleProfile
from velune.council.request import (
    CouncilRequest,
    CouncilSettings,
    EvidenceItem,
    ResponseRequirements,
)
from velune.council.results import StageResult
from velune.council.serialization import Contract
from velune.council.stages import StageContract, StageOutput, VisibilityPolicy
from velune.council.trace import TraceEventKind, TraceLog


class VisibilityViolation(Exception):
    """A stage tried to read something its contract does not grant."""


class StoredArtifact(Contract):
    kind: ArtifactKind
    stage: StageId
    author: str
    target: str | None = None
    payload: Any


class ClaimDigest(Contract):
    """Claims only, no prose: what a ``digest_all`` reader gets instead of the full artifact."""

    author: str
    claims: tuple[Claim, ...]


class ArtifactStore:
    """Append-only. Reads happen through ``StageView``; nothing here enforces visibility itself."""

    def __init__(self) -> None:
        self._items: list[StoredArtifact] = []

    def commit(self, items: tuple[StoredArtifact, ...]) -> None:
        self._items.extend(items)

    def all(self, kind: ArtifactKind) -> tuple[StoredArtifact, ...]:
        return tuple(item for item in self._items if item.kind is kind)

    def __len__(self) -> int:
        return len(self._items)


class StageView:
    """The one read path for a seat inside a stage."""

    def __init__(
        self,
        *,
        policy: VisibilityPolicy,
        stage: StageId,
        viewer: str,
        request: CouncilRequest,
        store: ArtifactStore,
        assignments: AssignmentSource | None = None,
    ) -> None:
        self._policy = policy
        self._stage = stage
        self._viewer = viewer
        self._request = request
        self._store = store
        self._assignments = assignments

    @property
    def viewer(self) -> str:
        return self._viewer

    @property
    def stage(self) -> StageId:
        return self._stage

    def _require(self, kind: ArtifactKind) -> tuple[ReadScope, ...]:
        scopes = self._policy.scopes(self._stage, kind)
        if not scopes:
            raise VisibilityViolation(
                f"stage {self._stage.value} may not read {kind.value} artifacts"
            )
        return scopes

    def question(self) -> str:
        self._require(ArtifactKind.QUESTION)
        return self._request.question

    def context(self) -> str:
        self._require(ArtifactKind.QUESTION)
        return self._request.context

    def requirements(self) -> ResponseRequirements:
        self._require(ArtifactKind.QUESTION)
        return self._request.response

    def evidence(self) -> tuple[EvidenceItem, ...]:
        self._require(ArtifactKind.EVIDENCE)
        return self._request.evidence

    def _assigned(self) -> tuple[str, ...]:
        if self._assignments is None:
            return ()
        return self._assignments.assigned_authors(self._stage, self._viewer)

    def read(self, kind: ArtifactKind, *, seat: str | None = None) -> tuple[StoredArtifact, ...]:
        """Full artifacts of ``kind`` this seat may see, optionally narrowed to one author.

        A kind granted only as a ``digest_all`` cannot be read in full; use :meth:`digest`.
        Asking for a specific author the scopes do not allow is a violation, not an empty result.
        """
        if kind in (ArtifactKind.QUESTION, ArtifactKind.EVIDENCE):
            raise VisibilityViolation(f"{kind.value} is request data; use the dedicated accessor")
        scopes = self._require(kind)
        full = [scope for scope in scopes if scope is not ReadScope.DIGEST_ALL]
        if not full:
            raise VisibilityViolation(
                f"stage {self._stage.value} may read only a digest of {kind.value} artifacts"
            )
        if seat is not None and not self._author_permitted(full, seat):
            raise VisibilityViolation(
                f"{self._viewer} may not read {kind.value} artifacts authored by {seat}"
            )
        visible = [
            item
            for item in self._store.all(kind)
            if (seat is None or item.author == seat) and self._item_permitted(full, item)
        ]
        return tuple(visible)

    def digest(self, kind: ArtifactKind) -> tuple[ClaimDigest, ...]:
        """A claims-only digest of everyone's ``kind`` artifacts, if the contract grants one."""
        if ReadScope.DIGEST_ALL not in self._require(kind):
            raise VisibilityViolation(
                f"stage {self._stage.value} has no digest access to {kind.value} artifacts"
            )
        allowed = self._digest_authors()
        out: list[ClaimDigest] = []
        for item in self._store.all(kind):
            if allowed is not None and item.author not in allowed:
                continue
            claims = getattr(item.payload, "claims", None)
            if claims:
                out.append(ClaimDigest(author=item.author, claims=tuple(claims)))
        return tuple(out)

    def _digest_authors(self) -> tuple[str, ...] | None:
        """Authors a digest may cover for this viewer, or ``None`` for no restriction."""
        if isinstance(self._assignments, DigestAssignmentSource):
            return self._assignments.digest_authors(self._stage, self._viewer)
        return None

    def _author_permitted(self, scopes: list[ReadScope], author: str) -> bool:
        for scope in scopes:
            if scope is ReadScope.ALL or scope is ReadScope.ADDRESSED_TO_SELF:
                return True
            if scope is ReadScope.OWN and author == self._viewer:
                return True
            if scope is ReadScope.ASSIGNED and author in self._assigned():
                return True
        return False

    def _item_permitted(self, scopes: list[ReadScope], item: StoredArtifact) -> bool:
        for scope in scopes:
            if scope is ReadScope.ALL:
                return True
            if scope is ReadScope.OWN and item.author == self._viewer:
                return True
            if scope is ReadScope.ADDRESSED_TO_SELF and item.target == self._viewer:
                return True
            if scope is ReadScope.ASSIGNED and item.author in self._assigned():
                return True
        return False


@dataclass
class CouncilState:
    """Mutable per-run holder, owned by the runner. Stages never receive it."""

    request: CouncilRequest
    profile: RoleProfile
    run_id: str
    trace: TraceLog
    store: ArtifactStore = field(default_factory=ArtifactStore)
    stage_results: list[StageResult] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    answer: str | None = None
    deterministic_fallback_used: bool = False

    def commit(self, stage: StageId, output: StageOutput) -> None:
        self.store.commit(
            tuple(
                StoredArtifact(
                    kind=item.kind,
                    stage=stage,
                    author=item.author,
                    target=item.target,
                    payload=item.payload,
                )
                for item in output.artifacts
            )
        )


@dataclass(frozen=True)
class StageContext:
    """Everything a stage gets. There is deliberately no provider, model or state object in it."""

    request: CouncilRequest
    profile: RoleProfile
    contract: StageContract
    invoker: SeatInvoker
    scheduler: Scheduler
    settings: CouncilSettings
    timeout_s: float
    view_factory: Callable[[str], StageView] = field(repr=False)
    emit_fn: Callable[..., None] = field(repr=False)

    def view_for(self, seat_id: str) -> StageView:
        return self.view_factory(seat_id)

    def emit(
        self,
        event: TraceEventKind,
        *,
        seat: str | None = None,
        status: str = "",
        detail: str = "",
    ) -> None:
        self.emit_fn(event, seat=seat, status=status, detail=detail)
