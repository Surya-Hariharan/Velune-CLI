"""Resource ownership: which application resources *this task* created.

The agent may freely manage what it created (start, stop, restart a container it
made) but must never assume an existing resource is its own. The ledger is the
single record of that distinction:

* ``OWNED``       — created by Velune in this session (recorded after success).
* ``PREEXISTING`` — observed before Velune touched anything (a snapshot).
* ``UNKNOWN``     — anything else: created by the user, another process, or a
                    different session. Treated exactly like ``PREEXISTING``.

Only ``OWNED`` resources count as the task's own. Connectors consult the ledger
in :meth:`~velune.resources.base.ResourceConnector.describe_actions` to raise the
risk of acting on anything else, so the permission gate demands a human
decision (AUTO mode included).

Thread-safe; in-memory for the session, with a JSON-safe ``to_dict`` /
``from_dict`` so later phases can persist it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from velune._compat import StrEnum


class Ownership(StrEnum):
    OWNED = "owned"
    PREEXISTING = "preexisting"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ResourceRef:
    """Identity of one application resource: ``docker`` / ``container`` / ``api-db``."""

    integration: str
    kind: str
    identifier: str

    def key(self) -> str:
        return f"{self.integration}:{self.kind}:{self.identifier}"


@dataclass(slots=True)
class _Entry:
    ref: ResourceRef
    ownership: Ownership
    session_id: str
    recorded_at: float = field(default_factory=time.time)


class OwnershipLedger:
    """Per-session record of created and pre-existing resources."""

    def __init__(self, session_id: str = "") -> None:
        self.session_id = session_id
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    def record_created(self, ref: ResourceRef) -> None:
        """Mark *ref* as created by this session (call only after success)."""
        with self._lock:
            self._entries[ref.key()] = _Entry(ref, Ownership.OWNED, self.session_id)

    def record_preexisting(self, refs: list[ResourceRef]) -> None:
        """Snapshot resources that existed before the task acted.

        Never downgrades an ``OWNED`` entry: a resource this session created
        stays its own even if a later snapshot sees it.
        """
        with self._lock:
            for ref in refs:
                entry = self._entries.get(ref.key())
                if entry is not None and entry.ownership is Ownership.OWNED:
                    continue
                self._entries[ref.key()] = _Entry(ref, Ownership.PREEXISTING, self.session_id)

    def release(self, ref: ResourceRef) -> None:
        """Forget *ref* (it was removed)."""
        with self._lock:
            self._entries.pop(ref.key(), None)

    def ownership(self, ref: ResourceRef) -> Ownership:
        with self._lock:
            entry = self._entries.get(ref.key())
        return entry.ownership if entry is not None else Ownership.UNKNOWN

    def is_owned(self, ref: ResourceRef) -> bool:
        return self.ownership(ref) is Ownership.OWNED

    def owned(self, integration: str | None = None, kind: str | None = None) -> list[ResourceRef]:
        with self._lock:
            entries = list(self._entries.values())
        return [
            e.ref
            for e in entries
            if e.ownership is Ownership.OWNED
            and (integration is None or e.ref.integration == integration)
            and (kind is None or e.ref.kind == kind)
        ]

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            entries = list(self._entries.values())
        return {
            "session_id": self.session_id,
            "entries": [
                {
                    "integration": e.ref.integration,
                    "kind": e.ref.kind,
                    "identifier": e.ref.identifier,
                    "ownership": e.ownership.value,
                    "session_id": e.session_id,
                    "recorded_at": e.recorded_at,
                }
                for e in entries
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, session_id: str | None = None) -> OwnershipLedger:
        """Rebuild a ledger. Entries owned by *another* session load as PREEXISTING.

        A resource created by a previous session is not this task's own: the
        ownership rule is per session, so resuming never widens what may be
        managed without asking.
        """
        sid = session_id if session_id is not None else str(data.get("session_id", ""))
        ledger = cls(sid)
        for raw in data.get("entries", []) if isinstance(data, dict) else []:
            try:
                ref = ResourceRef(str(raw["integration"]), str(raw["kind"]), str(raw["identifier"]))
                ownership = Ownership(str(raw.get("ownership", "unknown")))
                owner = str(raw.get("session_id", ""))
            except (KeyError, ValueError, TypeError):
                continue
            if ownership is Ownership.OWNED and owner == sid:
                ledger.record_created(ref)
            elif ownership in (Ownership.OWNED, Ownership.PREEXISTING):
                ledger.record_preexisting([ref])
        return ledger
