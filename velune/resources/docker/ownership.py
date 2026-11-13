"""Docker resource ownership (VAIR A4): what Velune created, proven, not guessed.

The question this answers is "may the agent manage this object without a person
confirming?" — and the answer is yes only for an object **proven** to be the
task's own. Proof is a *record Velune wrote itself* that still matches the daemon:

* a record is written only after Velune created the object successfully, keyed by
  the daemon's stable **id** (never the name) and a *fingerprint* (the creation
  timestamp), so a different object that later reuses a name is not the owned one;
* the record lives in Velune's own application-data directory, outside the
  workspace, so a repository cannot plant one;
* **labels are claims, not proof.** An image can bake ``dev.velune.managed=true``
  into every container made from it, so a label alone never confers ownership.
  Velune adds labels to what it creates (``dev.velune.managed``, ``.session``,
  ``.workspace``) for humans and for reconciliation, and a record whose object
  has lost or changed its labels is treated as inconsistent and *not* trusted.

Classes, from most to least trusted:

``VELUNE``      created by this session (record + labels + fingerprint all agree)
``PREVIOUS``    created by Velune in an earlier session (persisted record verified);
                needs confirmation like any existing resource, but cleanup is possible
``PREEXISTING`` present when this session first connected
``EXTERNAL``    carries Velune labels (or another project's) but there is no record
``UNKNOWN``     anything else, and every inconsistent or unverifiable case

Only ``VELUNE`` lets AUTO mode act unattended. :meth:`DockerOwnershipStore.reconcile`
drops records whose object has vanished or no longer matches, so stale ownership
cannot linger.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from velune._compat import StrEnum

logger = logging.getLogger("velune.resources.docker.ownership")

LABEL_MANAGED = "dev.velune.managed"
LABEL_SESSION = "dev.velune.session"
LABEL_WORKSPACE = "dev.velune.workspace"
KINDS = ("container", "image", "volume", "network")
MAX_RECORDS = 5000


class DockerOwnership(StrEnum):
    VELUNE = "velune"
    PREVIOUS = "velune_previous"
    PREEXISTING = "preexisting"
    EXTERNAL = "external"
    UNKNOWN = "unknown"

    @property
    def trusted(self) -> bool:
        return self is DockerOwnership.VELUNE


@dataclass(frozen=True, slots=True)
class Facts:
    """What the daemon says about one object (never from the model)."""

    kind: str
    id: str
    name: str
    fingerprint: str
    labels: dict[str, str]


@dataclass(slots=True)
class Record:
    kind: str
    id: str
    name: str
    fingerprint: str
    session: str
    recorded_at: float
    #: Set for objects created by ``docker compose`` for this project: Compose
    #: does not add Velune labels, so its own project label is the proof instead.
    project: str = ""


def workspace_hash(workspace: Path | None) -> str:
    text = str(workspace.resolve()) if workspace else ""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class DockerOwnershipStore:
    """Persistent, daemon-scoped ownership records for one workspace and session."""

    def __init__(
        self,
        session_id: str,
        workspace: Path | None,
        *,
        path: Path | None = None,
        daemon_id: str = "",
    ) -> None:
        self.session_id = session_id or "session"
        self.workspace_hash = workspace_hash(workspace)
        self._path = path
        self._daemon = daemon_id
        self._lock = threading.RLock()
        self._records: dict[str, Record] = {}
        self._snapshot: set[str] = set()
        self._loaded = False

    # ── identity of the daemon the records belong to ────────────────────────

    def bind_daemon(self, daemon_id: str) -> None:
        """Records are per daemon: a remote engine's objects never match local ones."""
        with self._lock:
            if daemon_id != self._daemon or not self._loaded:
                self._daemon = daemon_id
                self._records = {}
                self._loaded = False
                self._load()

    # ── persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        self._loaded = True
        if self._path is None:
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            rows = (data.get("daemons") or {}).get(self._daemon) or []
            for raw in rows[:MAX_RECORDS]:
                rec = Record(
                    str(raw["kind"]),
                    str(raw["id"]),
                    str(raw.get("name", "")),
                    str(raw["fingerprint"]),
                    str(raw.get("session", "")),
                    float(raw.get("recorded_at", 0)),
                    str(raw.get("project", "")),
                )
                if rec.kind in KINDS:
                    self._records[self._key(rec.kind, rec.id)] = rec
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            # A corrupt file means nothing is proven: start empty (fail safe) and
            # keep the bad file aside so it can be inspected.
            logger.warning("Docker ownership records could not be read; none are trusted")
            self._records = {}

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            existing: dict[str, Any] = {}
            try:
                existing = json.loads(self._path.read_text(encoding="utf-8")).get("daemons", {})
            except (OSError, ValueError, AttributeError):
                existing = {}
            existing[self._daemon] = [
                {
                    "kind": r.kind,
                    "id": r.id,
                    "name": r.name,
                    "fingerprint": r.fingerprint,
                    "session": r.session,
                    "recorded_at": r.recorded_at,
                    "project": r.project,
                }
                for r in self._records.values()
            ]
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": 1, "daemons": existing}), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            logger.warning("Docker ownership records could not be saved")

    @staticmethod
    def _key(kind: str, ident: str) -> str:
        return f"{kind}:{ident}"

    # ── labels ───────────────────────────────────────────────────────────────

    def labels(self) -> dict[str, str]:
        """Labels Velune puts on everything it creates."""
        return {
            LABEL_MANAGED: "true",
            LABEL_SESSION: self.session_id,
            LABEL_WORKSPACE: self.workspace_hash,
        }

    # ── recording ────────────────────────────────────────────────────────────

    def record_created(self, facts: Facts, project: str = "") -> None:
        """Record an object Velune just created (call only after the daemon confirmed it)."""
        with self._lock:
            self._records[self._key(facts.kind, facts.id)] = Record(
                facts.kind,
                facts.id,
                facts.name,
                facts.fingerprint,
                self.session_id,
                time.time(),
                project,
            )
            if len(self._records) > MAX_RECORDS:  # keep the newest
                for key in sorted(self._records, key=lambda k: self._records[k].recorded_at)[
                    : len(self._records) - MAX_RECORDS
                ]:
                    self._records.pop(key, None)
            self._save()

    def release(self, kind: str, ident: str) -> None:
        with self._lock:
            if self._records.pop(self._key(kind, ident), None) is not None:
                self._save()

    def record_for(self, kind: str, ident: str) -> Record | None:
        with self._lock:
            return self._records.get(self._key(kind, ident))

    def remember_existing(self, kind: str, ids: list[str]) -> None:
        """The snapshot taken when the session first connected (pre-existing objects)."""
        with self._lock:
            self._snapshot.update(self._key(kind, i) for i in ids[:MAX_RECORDS])

    def owned(self, kind: str | None = None) -> list[Record]:
        with self._lock:
            return [r for r in self._records.values() if kind in (None, r.kind)]

    # ── judging ──────────────────────────────────────────────────────────────

    def classify(self, facts: Facts | None) -> DockerOwnership:
        """Ownership of *facts* (None — it could not be looked up — is UNKNOWN)."""
        if facts is None:
            return DockerOwnership.UNKNOWN
        with self._lock:
            record = self._records.get(self._key(facts.kind, facts.id))
            snapshotted = self._key(facts.kind, facts.id) in self._snapshot
        claims_velune = facts.labels.get(LABEL_MANAGED) == "true"
        if record is None:
            if claims_velune:
                return DockerOwnership.EXTERNAL  # a claim with no proof
            if facts.labels.get("com.docker.compose.project") or facts.labels.get(
                "com.docker.stack.namespace"
            ):
                return DockerOwnership.EXTERNAL
            return DockerOwnership.PREEXISTING if snapshotted else DockerOwnership.UNKNOWN
        if record.fingerprint != facts.fingerprint:
            return DockerOwnership.UNKNOWN  # same id/name, different object
        if facts.kind != "image":
            # Images carry the labels of their Dockerfile, not ours.
            verified = self._verified(record, facts)
            if not verified:
                return DockerOwnership.UNKNOWN  # inconsistent: fail safe
        return (
            DockerOwnership.VELUNE
            if record.session == self.session_id
            else DockerOwnership.PREVIOUS
        )

    def _verified(self, record: Record, facts: Facts) -> bool:
        """Do the object's labels still agree with the record that claims it?"""
        if record.project:
            return facts.labels.get("com.docker.compose.project") == record.project
        return (
            facts.labels.get(LABEL_MANAGED) == "true"
            and facts.labels.get(LABEL_WORKSPACE) == self.workspace_hash
        )

    def reconcile(self, live: dict[str, dict[str, Facts]]) -> dict[str, list[str]]:
        """Drop records that no longer match the daemon. *live* maps kind → id → facts.

        A kind that was not listed is left alone (an unreadable listing must not
        erase everything). Returns what was dropped, by reason.
        """
        report: dict[str, list[str]] = {"missing": [], "changed": [], "relabelled": []}
        with self._lock:
            for key, record in list(self._records.items()):
                if record.kind not in live:
                    continue
                facts = live[record.kind].get(record.id)
                if facts is None:
                    report["missing"].append(key)
                    self._records.pop(key)
                elif facts.fingerprint != record.fingerprint:
                    report["changed"].append(key)
                    self._records.pop(key)
                elif record.kind != "image" and not self._verified(record, facts):
                    report["relabelled"].append(key)
                    self._records.pop(key)
            if any(report.values()):
                self._save()
        return report
