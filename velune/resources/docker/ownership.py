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

Integrity (VAIR A5). The record file is *authenticated*, not merely stored: each
daemon's records carry an HMAC-SHA256 computed with a random key kept in the encrypted
keystore (outside the workspace and outside the record directory), so a file edited or
forged by anything that lacks the key (a repository script, another local program, a
hand edit) fails verification and **nothing in it is trusted**. Encryption would not
help here: ownership is a claim to be *proven*, and a MAC proves "Velune wrote this".
A replayed older file can only restore records Velune genuinely wrote, and each is
still checked against the live daemon (id, fingerprint, labels) before it counts.
Writes are atomic (unique temp file, flush, fsync, replace), serialized across
processes by a lock, and merged with what other Velune processes recorded; a file
that is corrupt, unauthenticated or from an older format is set aside, never read.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Callable
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
FORMAT_VERSION = 2
#: Keystore id of the HMAC key. The ``resource:`` namespace keeps it out of provider ids.
LEDGER_KEY_ID = "resource:docker:ledger-key"


def default_ledger_key() -> bytes | None:
    """The ledger's HMAC key from the encrypted keystore (created on first use).

    ``None`` when the keystore cannot be used: the ledger then keeps records in memory
    only and trusts nothing from disk. It fails safe rather than falling back to an
    unauthenticated file.
    """
    try:
        from velune.providers.keystore import get_key, save_key

        raw = get_key(LEDGER_KEY_ID)
        if not raw:
            save_key(LEDGER_KEY_ID, secrets.token_hex(32))
            raw = get_key(LEDGER_KEY_ID)  # whichever process won a creation race
        return bytes.fromhex(raw) if raw else None
    except Exception:
        return None


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
        key_provider: Callable[[], bytes | None] | None = None,
    ) -> None:
        self.session_id = session_id or "session"
        self.workspace_hash = workspace_hash(workspace)
        self._path = path
        self._daemon = daemon_id
        self._lock = threading.RLock()
        self._records: dict[str, Record] = {}
        self._snapshot: set[str] = set()
        self._loaded = False
        self._key_provider = key_provider or default_ledger_key
        self._key: bytes | None = None
        self._key_checked = False
        #: Records removed in this process: a merge with disk must not resurrect them.
        self._removed: set[str] = set()

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

    def _hmac_key(self) -> bytes | None:
        if not self._key_checked:
            self._key_checked = True
            try:
                self._key = self._key_provider()
            except Exception:
                self._key = None
            if self._key is None:
                logger.warning("Docker ownership records cannot be authenticated; not persisted")
        return self._key

    @staticmethod
    def _row(r: Record) -> dict[str, Any]:
        return {
            "kind": r.kind,
            "id": r.id,
            "name": r.name,
            "fingerprint": r.fingerprint,
            "session": r.session,
            "recorded_at": r.recorded_at,
            "project": r.project,
        }

    def _mac(self, key: bytes, daemon: str, rows: list[dict[str, Any]]) -> str:
        """HMAC over the daemon, this workspace and the records in a canonical order."""
        body = json.dumps(
            {
                "v": FORMAT_VERSION,
                "daemon": daemon,
                "workspace": self.workspace_hash,
                "records": sorted(rows, key=lambda r: (str(r.get("kind")), str(r.get("id")))),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hmac.new(key, body.encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def _record_from(raw: dict[str, Any]) -> Record | None:
        try:
            rec = Record(
                str(raw["kind"]),
                str(raw["id"]),
                str(raw.get("name", "")),
                str(raw["fingerprint"]),
                str(raw.get("session", "")),
                float(raw.get("recorded_at", 0)),
                str(raw.get("project", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None
        return rec if rec.kind in KINDS else None

    def _quarantine(self, reason: str) -> None:
        """Set a file we must not trust aside, so it can be inspected but never read."""
        if self._path is None:
            return
        target = self._path.with_name(f"{self._path.name}.{reason}-{int(time.time())}")
        with contextlib.suppress(OSError):
            self._path.replace(target)
        logger.warning(
            "Docker ownership records set aside (%s); none from that file are trusted", reason
        )

    def _read_daemon(self) -> dict[str, Record] | None:
        """Verified records for this daemon, ``{}`` when none, ``None`` when untrusted."""
        if self._path is None:
            return {}
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError:
            return None
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except ValueError:
            self._quarantine("corrupt")
            return None
        if data.get("version") != FORMAT_VERSION:
            # An older, unauthenticated format cannot be trusted: set it aside.
            self._quarantine("unauthenticated")
            return None
        daemons = data.get("daemons")
        section = daemons.get(self._daemon) if isinstance(daemons, dict) else None
        if section is None:
            return {}
        key = self._hmac_key()
        rows = section.get("records") if isinstance(section, dict) else None
        mac = section.get("mac") if isinstance(section, dict) else None
        if key is None or not isinstance(rows, list) or not isinstance(mac, str):
            return None
        rows = [r for r in rows[:MAX_RECORDS] if isinstance(r, dict)]
        if not hmac.compare_digest(mac, self._mac(key, self._daemon, rows)):
            self._quarantine("tampered")
            return None
        out: dict[str, Record] = {}
        for raw in rows:
            rec = self._record_from(raw)
            if rec is not None:
                out[self._key_of(rec.kind, rec.id)] = rec
        return out

    def _load(self) -> None:
        self._loaded = True
        if self._path is None:
            return
        verified = self._read_daemon()
        # Unverifiable means nothing is proven: start empty (fail safe).
        self._records = dict(verified) if verified else {}

    def _save(self) -> None:
        if self._path is None:
            return
        key = self._hmac_key()
        if key is None:
            return  # cannot authenticate: keep records in memory only
        from velune.core.filelock import FileLock, FileLockTimeout

        lock = FileLock(self._path)
        try:
            lock.acquire(timeout=5.0)
        except (FileLockTimeout, OSError):
            logger.warning("Docker ownership records could not be saved (locked)")
            return
        try:
            # Merge with what other Velune processes recorded since we loaded.
            on_disk = self._read_daemon() or {}
            merged = {k: v for k, v in on_disk.items() if k not in self._removed}
            merged.update(self._records)
            self._records = merged
            rows = [self._row(r) for r in merged.values()]
            sections: dict[str, Any] = {}
            try:
                existing = json.loads(self._path.read_text(encoding="utf-8"))
                if existing.get("version") == FORMAT_VERSION:
                    sections = dict(existing.get("daemons") or {})
            except (OSError, ValueError, AttributeError):
                sections = {}
            sections[self._daemon] = {"records": rows, "mac": self._mac(key, self._daemon, rows)}
            self._write_atomic({"version": FORMAT_VERSION, "daemons": sections})
        except OSError:
            logger.warning("Docker ownership records could not be saved")
        finally:
            lock.release()

    def _write_atomic(self, document: dict[str, Any]) -> None:
        """Unique temp file in the same directory, flushed and fsynced, then replaced."""
        assert self._path is not None
        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            with contextlib.suppress(OSError):
                os.chmod(directory, 0o700)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=self._path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":  # owner-only; on Windows the profile ACL applies
                os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    @staticmethod
    def _key_of(kind: str, ident: str) -> str:
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
            self._removed.discard(self._key_of(facts.kind, facts.id))
            self._records[self._key_of(facts.kind, facts.id)] = Record(
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
            key = self._key_of(kind, ident)
            if self._records.pop(key, None) is not None:
                self._removed.add(key)
                self._save()

    def record_for(self, kind: str, ident: str) -> Record | None:
        with self._lock:
            return self._records.get(self._key_of(kind, ident))

    def remember_existing(self, kind: str, ids: list[str]) -> None:
        """The snapshot taken when the session first connected (pre-existing objects)."""
        with self._lock:
            self._snapshot.update(self._key_of(kind, i) for i in ids[:MAX_RECORDS])

    def owned(self, kind: str | None = None) -> list[Record]:
        with self._lock:
            return [r for r in self._records.values() if kind in (None, r.kind)]

    # ── judging ──────────────────────────────────────────────────────────────

    def classify(self, facts: Facts | None) -> DockerOwnership:
        """Ownership of *facts* (None — it could not be looked up — is UNKNOWN)."""
        if facts is None:
            return DockerOwnership.UNKNOWN
        with self._lock:
            record = self._records.get(self._key_of(facts.kind, facts.id))
            snapshotted = self._key_of(facts.kind, facts.id) in self._snapshot
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
                    self._removed.add(key)
                elif facts.fingerprint != record.fingerprint:
                    report["changed"].append(key)
                    self._records.pop(key)
                    self._removed.add(key)
                elif record.kind != "image" and not self._verified(record, facts):
                    report["relabelled"].append(key)
                    self._records.pop(key)
                    self._removed.add(key)
            if any(report.values()):
                self._save()
        return report
