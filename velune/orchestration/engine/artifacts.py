"""The trusted side of a run: the artifact store and the only builder of agent context.

The driver is the authorization boundary, not the runner. Everything an agent is shown comes from
here, in three steps:

1. Only driver code calls :meth:`ArtifactStore.put`. It stamps ``kind``, ``author``, ``target``,
   ``phase`` and ``attempt`` itself, from the step it is running, never from anything a model said.
2. :class:`ContextBuilder` is the only producer of ``ContextItem`` the driver uses. It applies each
   of the viewer's read grants, including the three that need driver-side data (``ASSIGNED``,
   ``ADDRESSED_TO_SELF`` and ``DIGEST_ALL``), and anything not granted is dropped (fail closed).
3. Model output enters the store as *text only*; its labels are decided by the driver.

Nothing here awaits, calls a provider, or touches a disk.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from pydantic import Field, model_validator

from velune._compat import StrEnum
from velune.core.types.agent import AgentRole
from velune.council.domain import ArtifactKind, ReadScope
from velune.council.ports import IdSource
from velune.council.serialization import Contract
from velune.orchestration.engine.runner import ContextItem
from velune.orchestration.engine.spec import AgentSpec
from velune.orchestration.state.run_state import RunPhase

DIGEST_CHARS = 200


class ArtifactOrigin(StrEnum):
    """Where an artifact's content came from. Assigned by engine code, never read from content.

    ``CALLER`` is material the caller supplied (the task), ``MODEL`` is text an agent wrote, and
    ``TOOL`` is data a tool returned. Tool data is untrusted like model text, but it is a different
    thing, so it is never mistaken for a model's statement or relabelled as one.
    """

    CALLER = "caller"
    MODEL = "model"
    TOOL = "tool"


class StoredArtifact(Contract):
    """One thing the run produced or was given, with driver-stamped metadata.

    ``author`` is ``None`` for material the caller supplied (the task itself) and for tool data.
    """

    artifact_id: str
    kind: ArtifactKind
    author: AgentRole | None = None
    target: AgentRole | None = None
    phase: RunPhase
    attempt: int = Field(ge=1)
    text: str = Field(min_length=1)
    digest: str
    origin: ArtifactOrigin

    @model_validator(mode="after")
    def _origin_matches_author(self) -> StoredArtifact:
        if self.origin is ArtifactOrigin.MODEL and self.author is None:
            raise ValueError("model-origin text must have an author")
        if self.origin is not ArtifactOrigin.MODEL and self.author is not None:
            raise ValueError(f"{self.origin.value}-origin data cannot claim an agent author")
        return self


@runtime_checkable
class ArtifactReader(Protocol):
    """Read-only view of a store, which is all the context builder is given."""

    def artifacts(self) -> tuple[StoredArtifact, ...]: ...


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class ArtifactStore:
    """An in-memory, append-only list of :class:`StoredArtifact` for one run."""

    def __init__(self, ids: IdSource) -> None:
        self._ids = ids
        self._items: list[StoredArtifact] = []

    def put(
        self,
        *,
        kind: ArtifactKind,
        text: str,
        author: AgentRole | None,
        phase: RunPhase,
        attempt: int,
        target: AgentRole | None = None,
        origin: ArtifactOrigin | None = None,
    ) -> StoredArtifact:
        """Append an artifact. ``origin`` is the engine's claim about provenance, never the content's.

        When omitted it follows the author: an agent author means model text, none means caller
        material. Tool data must be stored with ``origin=ArtifactOrigin.TOOL`` and no author.
        """
        item = StoredArtifact(
            artifact_id=self._ids.next_id("artifact"),
            kind=kind,
            author=author,
            target=target,
            phase=phase,
            attempt=attempt,
            text=text,
            digest=text_digest(text),
            origin=origin
            if origin is not None
            else (ArtifactOrigin.MODEL if author is not None else ArtifactOrigin.CALLER),
        )
        self._items.append(item)
        return item

    def artifacts(self) -> tuple[StoredArtifact, ...]:
        return tuple(self._items)

    def latest(
        self, kind: ArtifactKind, *, author: AgentRole | None = None
    ) -> StoredArtifact | None:
        for item in reversed(self._items):
            if item.kind is kind and (author is None or item.author is author):
                return item
        return None


@runtime_checkable
class AssignmentPolicy(Protocol):
    """Which authors' artifacts a viewer is assigned to read. Trusted, driver-side data."""

    def assigned_authors(self, viewer: AgentRole) -> tuple[AgentRole, ...]: ...


class StaticAssignments:
    """A fixed viewer to authors map. Anyone not listed is assigned nobody (fail closed)."""

    def __init__(self, mapping: Mapping[AgentRole, tuple[AgentRole, ...]] | None = None) -> None:
        self._mapping = dict(mapping or {})

    def assigned_authors(self, viewer: AgentRole) -> tuple[AgentRole, ...]:
        return tuple(self._mapping.get(viewer, ()))


def digest_of(text: str) -> str:
    """A bounded excerpt standing in for the full text: the first non-empty line, truncated."""
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:DIGEST_CHARS]
    return text.strip()[:DIGEST_CHARS]


class ContextBuilder:
    """Turns store contents into the context one agent may see, and nothing more."""

    def __init__(self, assignments: AssignmentPolicy | None = None) -> None:
        self._assignments = assignments or StaticAssignments()

    def build(self, spec: AgentSpec, store: ArtifactReader) -> tuple[ContextItem, ...]:
        assigned = frozenset(self._assignments.assigned_authors(spec.role))
        items: list[ContextItem] = []
        for artifact in store.artifacts():
            scope = spec.scope_for(artifact.kind)
            if not self._visible(scope, artifact, spec.role, assigned):
                continue
            text = digest_of(artifact.text) if scope is ReadScope.DIGEST_ALL else artifact.text
            # kind and author come from the stored, driver-stamped record, never from the text.
            items.append(ContextItem(kind=artifact.kind, text=text, author=artifact.author))
        return tuple(items)

    @staticmethod
    def _visible(
        scope: ReadScope,
        artifact: StoredArtifact,
        viewer: AgentRole,
        assigned: frozenset[AgentRole],
    ) -> bool:
        if scope is ReadScope.NONE:
            return False
        if scope is ReadScope.ALL or scope is ReadScope.DIGEST_ALL:
            return True
        if scope is ReadScope.OWN:
            return artifact.author is viewer
        if scope is ReadScope.ADDRESSED_TO_SELF:
            return artifact.target is viewer
        if scope is ReadScope.ASSIGNED:
            return artifact.author is not None and artifact.author in assigned
        return False  # an unknown scope grants nothing
