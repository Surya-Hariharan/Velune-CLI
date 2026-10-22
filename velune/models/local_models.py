"""Persisted local-model registrations, kept apart from runtime availability.

The discovered-model cache (``model_registry_cache.json``) is a snapshot of
what the last scan saw; it was also the only record of a local model. Unplug
the external SSD holding the Ollama store, or stop the daemon, and the next
scan saw nothing — so the model, and with it the user's chosen active model,
disappeared and had to be set up again when the drive came back.

A *registration* is the user's intent: "this model, served by this endpoint,
stored at this path". It is written when a local model is activated or
registered by path, and it is never removed because something is unavailable —
only by an explicit ``/model remove``. Each record carries the last known
metadata, so the model can still be listed while it is offline, plus its
current availability, when it was last verified, and the classified reason for
the most recent failure. Re-checking it (:func:`check_registration`) is cheap:
one ``stat`` of the storage path and one ``/api/tags`` request.

Stored at ``~/.velune/local_models.json`` with the same atomic-write and
"never overwrite an unreadable file" rules as the credential store.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from velune._compat import StrEnum
from velune.core.filelock import locked

logger = logging.getLogger("velune.models.local_models")

DEFAULT_PATH = Path.home() / ".velune" / "local_models.json"
_SCHEMA_VERSION = 1


class Availability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class FailureKind(StrEnum):
    """Why a registered local model cannot be used right now."""

    #: The model's storage path (drive, folder, file) is not accessible.
    STORAGE_UNAVAILABLE = "storage_unavailable"
    #: The model server (Ollama) did not answer at its endpoint.
    ENDPOINT_UNREACHABLE = "endpoint_unreachable"
    #: The server answered too slowly.
    TIMEOUT = "timeout"
    #: Server and storage are fine, but the server does not have this model.
    MODEL_MISSING = "model_missing"
    #: The model is on disk, but the running server is serving a different store.
    NOT_SERVED = "not_served"
    UNKNOWN = "unknown"


#: What the user can do next, per failure kind (rendered under the message).
NEXT_ACTIONS: dict[FailureKind, str] = {
    FailureKind.STORAGE_UNAVAILABLE: (
        "Reconnect the drive and run /model recheck, or point Velune at the new "
        "location with /model relocate <model> <path>. Other providers keep working."
    ),
    FailureKind.ENDPOINT_UNREACHABLE: (
        "Start the server (ollama serve), or set OLLAMA_HOST to where it runs, then "
        "run /model recheck. Pick another model with /model meanwhile."
    ),
    FailureKind.TIMEOUT: "The server is slow to answer — retry with /model recheck.",
    FailureKind.MODEL_MISSING: (
        "The server is running but no longer has this model. Pull it again "
        "(/pull <model>), register it from a file (/model register <path>), "
        "or choose another with /model."
    ),
    FailureKind.NOT_SERVED: (
        "Restart Ollama with OLLAMA_MODELS set to the store shown above, then run /model recheck."
    ),
    FailureKind.UNKNOWN: "Run /model recheck to try again, or choose another model with /model.",
}


class LocalModelStoreError(OSError):
    """The registrations file exists but cannot be read; it is left untouched."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def path_accessible(path: str | os.PathLike[str] | None) -> bool:
    """True if *path* exists right now. Never raises.

    On Windows a disconnected removable or mapped drive raises ``OSError``
    instead of returning False; that is treated as "not accessible".
    """
    if not path:
        return False
    try:
        return Path(path).expanduser().exists()
    except OSError:
        return False


@dataclass
class LocalModelRecord:
    """One registered local model. See the module docstring."""

    provider_id: str
    model_id: str
    endpoint: str | None = None
    #: Ollama store root, GGUF file, or model directory — whatever the model lives on.
    source_path: str | None = None
    #: "ollama_store" | "gguf_file" | "safetensors_dir" | None (unknown)
    source_kind: str | None = None
    #: Last known descriptor fields (context_length, display_name, family, ...).
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str = Availability.UNKNOWN
    last_verified: str | None = None
    last_checked: str | None = None
    last_error: str | None = None
    last_error_kind: str | None = None
    registered_at: str = field(default_factory=_now)
    #: "active" (remembered because it was activated) | "user" (registered by path)
    origin: str = "active"

    @property
    def key(self) -> tuple[str, str]:
        return self.provider_id, self.model_id

    @property
    def available(self) -> bool:
        return self.status == Availability.AVAILABLE

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LocalModelRecord:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in raw.items() if k in known})

    def failure_message(self) -> str:
        """A one-line, user-facing description of why the model is unavailable."""
        return self.last_error or f"{self.model_id} is unavailable."

    def next_action(self) -> str:
        try:
            kind = FailureKind(self.last_error_kind or FailureKind.UNKNOWN)
        except ValueError:
            kind = FailureKind.UNKNOWN
        return NEXT_ACTIONS[kind]


class LocalModelRegistry:
    """Load/save local-model registrations (``~/.velune/local_models.json``)."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DEFAULT_PATH

    @property
    def path(self) -> Path:
        return self._path

    # -- persistence ----------------------------------------------------

    def _read(self) -> dict[tuple[str, str], LocalModelRecord]:
        """Parse the file; raises :class:`LocalModelStoreError` if it is unreadable."""
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            items = data.get("models", []) if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise ValueError("no model list")
            records = [LocalModelRecord.from_dict(item) for item in items if isinstance(item, dict)]
        except (OSError, ValueError, TypeError) as exc:
            raise LocalModelStoreError(
                f"Local model registrations at {self._path} could not be read ({exc}); "
                "the file was left untouched."
            ) from exc
        return {r.key: r for r in records}

    def load(self) -> list[LocalModelRecord]:
        """All registrations; an unreadable file reads as none (and is not overwritten)."""
        try:
            return list(self._read().values())
        except LocalModelStoreError as exc:
            logger.warning("%s", exc)
            return []

    def _write(self, records: dict[tuple[str, str], LocalModelRecord]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _SCHEMA_VERSION,
            "models": [asdict(r) for r in sorted(records.values(), key=lambda r: r.key)],
        }
        tmp = self._path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2))
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(self._path)

    def _mutate(self, fn) -> None:
        """Read-modify-write under a cross-process lock. Raises if the file is unreadable."""
        with locked(self._path):
            records = self._read()
            fn(records)
            self._write(records)

    # -- queries ---------------------------------------------------------

    def get(self, provider_id: str, model_id: str) -> LocalModelRecord | None:
        for record in self.load():
            if record.key == (provider_id, model_id):
                return record
        return None

    # -- mutation --------------------------------------------------------

    def upsert(self, record: LocalModelRecord) -> None:
        """Insert or replace *record*, keeping the original registration time and origin."""

        def _apply(records: dict[tuple[str, str], LocalModelRecord]) -> None:
            existing = records.get(record.key)
            if existing is not None:
                record.registered_at = existing.registered_at
                if existing.origin == "user":
                    record.origin = "user"
            records[record.key] = record

        self._mutate(_apply)

    def remove(self, provider_id: str, model_id: str) -> bool:
        removed = []

        def _apply(records: dict[tuple[str, str], LocalModelRecord]) -> None:
            if records.pop((provider_id, model_id), None) is not None:
                removed.append(True)

        self._mutate(_apply)
        return bool(removed)

    def update_status(
        self,
        provider_id: str,
        model_id: str,
        *,
        available: bool,
        kind: FailureKind | None = None,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
        endpoint: str | None = None,
        source_path: str | None = None,
        source_kind: str | None = None,
    ) -> None:
        """Record a check result. Never deletes the registration or its saved config."""

        def _apply(records: dict[tuple[str, str], LocalModelRecord]) -> None:
            record = records.get((provider_id, model_id))
            if record is None:
                return
            record.last_checked = _now()
            if available:
                record.status = Availability.AVAILABLE
                record.last_verified = record.last_checked
                record.last_error = None
                record.last_error_kind = None
                if metadata:
                    record.metadata.update(metadata)
                if endpoint:
                    record.endpoint = endpoint
                if source_path:
                    record.source_path = source_path
                if source_kind:
                    record.source_kind = source_kind
            else:
                record.status = Availability.UNAVAILABLE
                record.last_error = (reason or "")[:300] or None
                record.last_error_kind = str(kind or FailureKind.UNKNOWN)

        self._mutate(_apply)


# ---------------------------------------------------------------------------
# Building records from descriptors, and descriptors from records
# ---------------------------------------------------------------------------

#: Descriptor fields copied into a registration so the model can be listed offline.
_REMEMBERED_FIELDS = (
    "display_name",
    "context_length",
    "quantization",
    "parameter_count_b",
    "vram_required_gb",
    "family",
    "speed_tier",
)


def record_from_descriptor(model, *, origin: str = "active") -> LocalModelRecord:
    """A registration capturing everything needed to show and recheck *model*."""
    from velune.providers.endpoints import ollama_base_url

    meta = dict(getattr(model, "metadata", {}) or {})
    remembered = {f: getattr(model, f, None) for f in _REMEMBERED_FIELDS}
    remembered = {k: v for k, v in remembered.items() if v is not None}
    root = meta.get("root") or meta.get("source_path")
    source_kind = meta.get("source_kind") or ("ollama_store" if meta.get("root") else None)
    endpoint = None
    if model.provider_id == "ollama":
        location = getattr(model, "location", None) or ""
        endpoint = location if location.startswith(("http://", "https://")) else ollama_base_url()
    available = meta.get("availability", Availability.AVAILABLE) == Availability.AVAILABLE
    return LocalModelRecord(
        provider_id=model.provider_id,
        model_id=model.model_id,
        endpoint=endpoint,
        source_path=str(root) if root else None,
        source_kind=source_kind,
        metadata=remembered,
        status=Availability.AVAILABLE if available else Availability.UNAVAILABLE,
        last_verified=_now() if available else None,
        origin=origin,
    )


def descriptor_from_record(record: LocalModelRecord):
    """A ModelDescriptor for a registration whose model is not currently discovered."""
    from velune.core.types.model import ModelCapabilityProfile, ModelDescriptor

    meta = record.metadata
    descriptor = ModelDescriptor(
        model_id=record.model_id,
        provider_id=record.provider_id,
        display_name=meta.get("display_name") or record.model_id,
        context_length=int(meta.get("context_length") or 8192),
        capabilities=ModelCapabilityProfile(),
        is_local=True,
        quantization=meta.get("quantization"),
        parameter_count_b=meta.get("parameter_count_b"),
        vram_required_gb=meta.get("vram_required_gb"),
        speed_tier=meta.get("speed_tier") or "medium",
        family=meta.get("family"),
        location=record.endpoint or record.source_path,
        health="offline" if record.status == Availability.UNAVAILABLE else "unknown",
        tags=["local", record.provider_id, "registered"],
        metadata={
            "registered": True,
            "availability": record.status,
            "unavailable_reason": record.last_error,
            "unavailable_kind": record.last_error_kind,
            "root": record.source_path,
            "source_kind": record.source_kind,
            "capabilities_known": False,
        },
    )
    return descriptor


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    available: bool
    kind: FailureKind | None = None
    reason: str = ""
    #: Fresh metadata from the server when it served the model.
    metadata: dict[str, Any] = field(default_factory=dict)
    endpoint: str | None = None


def _stored_names(root: str) -> set[str]:
    from velune.providers.ollama_store import OllamaModelStore

    try:
        return {m.name for m in OllamaModelStore(root).list_models()}
    except OSError:
        return set()


async def _ollama_tags(base: str, timeout: float) -> tuple[set[str] | None, FailureKind | None]:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{base}/api/tags")
        resp.raise_for_status()
        data = resp.json()
    except httpx.TimeoutException:
        return None, FailureKind.TIMEOUT
    except (httpx.HTTPError, ValueError):
        return None, FailureKind.ENDPOINT_UNREACHABLE
    return {
        m.get("name") or m.get("model") for m in data.get("models", []) if isinstance(m, dict)
    }, None


async def check_registration(record: LocalModelRecord, *, timeout: float = 3.0) -> CheckResult:
    """Classify whether *record*'s model can be used right now.

    Order matters for the message: a missing storage path is the most
    actionable cause ("reconnect the drive"), so it wins over an unreachable
    server when both hold. No directory walks beyond the registered store's
    manifests, no downloads, no model loading.
    """
    if record.provider_id != "ollama":
        # Registrations for runtimes Velune cannot probe: only the file/path.
        if record.source_path and not path_accessible(record.source_path):
            return CheckResult(
                False,
                FailureKind.STORAGE_UNAVAILABLE,
                f"Model storage path is unavailable: {record.source_path}",
            )
        return CheckResult(True)

    from velune.providers.endpoints import ollama_base_url

    base = ollama_base_url()
    storage_ok = path_accessible(record.source_path) if record.source_path else True
    names, failure = await _ollama_tags(base, timeout)

    if names is not None and record.model_id in names:
        return CheckResult(True, endpoint=base)
    if not storage_ok:
        return CheckResult(
            False,
            FailureKind.STORAGE_UNAVAILABLE,
            f"Model storage path is unavailable: {record.source_path}",
        )
    if failure is FailureKind.TIMEOUT:
        return CheckResult(False, failure, f"Ollama at {base} did not answer in time.")
    if failure is not None:
        return CheckResult(False, failure, f"Ollama server is not reachable at {base}.")
    if (
        record.source_path
        and record.source_kind == "ollama_store"
        and record.model_id in _stored_names(record.source_path)
    ):
        return CheckResult(
            False,
            FailureKind.NOT_SERVED,
            f"{record.model_id} is on disk at {record.source_path}, but the Ollama server "
            f"at {base} is serving a different model store.",
        )
    return CheckResult(
        False,
        FailureKind.MODEL_MISSING,
        f"Ollama at {base} is running but does not have {record.model_id}.",
    )


class RecheckThrottle:
    """Bounds automatic rechecks: at most one per model per *interval* seconds."""

    def __init__(self, interval: float = 10.0) -> None:
        self._interval = interval
        self._last: dict[tuple[str, str], float] = {}

    def allow(self, key: tuple[str, str], *, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and now - self._last.get(key, -1e9) < self._interval:
            return False
        self._last[key] = now
        return True
