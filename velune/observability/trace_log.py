"""Persistent, bounded, secret-redacted store for the execution event stream.

The :class:`~velune.events.CognitiveBus` keeps only an in-memory history that
dies with the process, so ``velune trace`` — invoked as a *separate* process
from the session that produced the work — would otherwise have nothing to read.
:class:`TraceLog` closes that gap: a session-side sink appends each real emitted
event here as one JSON line, and the command reads it back.

Design choices that keep this trustworthy and cheap:

* **Append-only JSONL** — one line per event, so a tail read is O(recent) and a
  crash mid-write at most loses the final partial line.
* **Bounded** — the file is trimmed to ``max_entries`` lines, so a long-running
  session cannot grow it without limit (Phase 9: bounded trace buffers).
* **Redacted at write time** — every stored value passes through
  :func:`velune.core.redaction.redact_secrets`, so a leaked key in event data is
  scrubbed before it ever hits disk.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from velune.core.redaction import redact_secrets

logger = logging.getLogger("velune.observability.trace_log")

DEFAULT_MAX_ENTRIES = 2000
TRACE_FILENAME = "trace.jsonl"


def _redact(value: Any) -> Any:
    """Recursively scrub secrets from a JSON-able value."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


class TraceLog:
    """A bounded, redacted JSONL store of execution events for one workspace."""

    def __init__(self, path: Path, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self.path = Path(path)
        self.max_entries = max_entries
        # Appends can arrive from the event-loop thread (bus sink) and agent
        # worker threads (orchestrator progress) concurrently; serialize them so
        # JSON lines never interleave.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Write side (used by the session-side sink)
    # ------------------------------------------------------------------

    def append(self, entry: dict[str, Any]) -> None:
        """Append one redacted event record, trimming the file if it grew too large.

        Best-effort: any I/O error is logged and swallowed so tracing never
        breaks the run it is observing.
        """
        record = _redact(entry)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError as exc:
                logger.debug("Could not append trace entry: %s", exc)
                return
            self._maybe_trim()

    def _maybe_trim(self) -> None:
        """Trim the file to ``max_entries`` lines once it overshoots.

        Trimming is amortized: we only rewrite when the line count exceeds the
        cap by 25%, so steady-state appends stay O(1).
        """
        try:
            with open(self.path, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return
        if len(lines) <= int(self.max_entries * 1.25):
            return
        keep = lines[-self.max_entries :]
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(keep)
            tmp.replace(self.path)
        except OSError as exc:
            logger.debug("Could not trim trace log: %s", exc)

    # ------------------------------------------------------------------
    # Read side (used by `velune trace`)
    # ------------------------------------------------------------------

    def read_recent(
        self,
        limit: int = 50,
        *,
        type_filter: str | None = None,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return up to *limit* most-recent events, oldest-first within the slice.

        ``type_filter`` matches against ``event_type`` as a case-insensitive
        substring; ``run_id`` matches ``correlation_id`` exactly. Malformed
        lines are skipped, never raised.
        """
        if not self.path.exists():
            return []
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = f.readlines()
        except OSError:
            return []

        records: list[dict[str, Any]] = []
        for line in raw:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            if type_filter and type_filter.lower() not in str(rec.get("event_type", "")).lower():
                continue
            if run_id and rec.get("correlation_id") != run_id:
                continue
            records.append(rec)

        return records[-limit:]

    def count(self) -> int:
        """Total stored events (best-effort; 0 if unreadable)."""
        if not self.path.exists():
            return 0
        try:
            with open(self.path, encoding="utf-8") as f:
                return sum(1 for line in f if line.strip())
        except OSError:
            return 0

    def clear(self) -> None:
        """Delete the trace log file if present."""
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("Could not clear trace log: %s", exc)
