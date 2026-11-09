"""Lifecycle journal: what an operation intends to change, written *before* it does.

If Velune (or the machine) dies half-way through a future update, the journal is
how the next run learns what the previous installation looked like. It exists
because uv overwrites its own receipt on reinstall (P2.0), so the previous
version, pin and extras would otherwise be lost.

This module is the data model and persistence only. It does not run uv, decide
what to do after an interruption, or roll anything back. A command may advance
the phase of, or clear, only the journal it wrote itself.

Durability, stated honestly
---------------------------
Writes go to a temporary file in the same directory, are flushed and fsynced,
and are then moved into place with :func:`os.replace`. That gives readers
either the old or the new file on the platforms where ``os.replace`` is atomic,
but power-loss durability and Windows behaviour under antivirus/indexer locks
are **not** tested here. A reader must therefore still cope with a missing,
partial or malformed file; :func:`read_journal` never raises on bad content.

A journal is never overwritten silently: an existing file means an earlier
operation may not have finished, and the next step is a human or ``repair``
decision, not a new write.

Concurrency: unsupported, but guarded
-------------------------------------
Two lifecycle commands must not run at once. The journal is created with an
*exclusive* filesystem operation (a hard link, or ``O_CREAT | O_EXCL`` where hard
links are unavailable), so exactly one writer can ever create it; the other gets
a ``BUSY`` refusal and nothing is changed. It is not a lock: there is no lock file
to go stale. A crash leaves the journal where it is for a person to look at.
Each journal also carries a random ``token`` so two operations that start in the
same second are still told apart.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from velune.core.paths import app_data_root
from velune.core.redaction import redact_secrets
from velune.lifecycle.errors import ExitCode, LifecycleError
from velune.lifecycle.ownership import OwnershipReport

SCHEMA_VERSION = 1
JOURNAL_FILENAME = "journal.json"
OPERATIONS = ("update", "repair", "uninstall")
PHASES = ("planned", "applying")
_MAX_JOURNAL_BYTES = 64 * 1024


@dataclass(frozen=True)
class JournalEntry:
    """The pre-operation state needed to recover or explain an interrupted operation."""

    operation: str
    phase: str
    started_at: str
    from_version: str | None
    specifier: str | None  # uv pin recorded in the receipt, e.g. "==0.9.9"
    extras: tuple[str, ...] = ()
    python_version: str | None = None  # running interpreter, e.g. "3.12.4"
    python_base: str | None = None  # base interpreter recorded by uv
    to_version: str | None = None
    installer: str = "uv"
    schema_version: int = field(default=SCHEMA_VERSION)
    # Random per-operation id (None in journals written before it existed). Not part of
    # equality: it identifies *which* operation wrote a file, see ``_is_ours``.
    token: str | None = field(default=None, compare=False)

    def to_json_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["extras"] = list(self.extras)
        if data.get("token") is None:
            data.pop("token", None)
        return data


@dataclass(frozen=True)
class JournalRead:
    """Result of reading a journal. ``state`` is missing | ok | incomplete | malformed."""

    state: str
    entry: JournalEntry | None = None
    problem: str | None = None


def default_journal_path() -> Path:
    """``<app data root>/lifecycle/journal.json`` (outside the tool environment)."""
    return app_data_root() / "lifecycle" / JOURNAL_FILENAME


def new_entry(
    operation: str,
    report: OwnershipReport,
    *,
    python_version: str,
    to_version: str | None = None,
    now: datetime | None = None,
) -> JournalEntry:
    """Build a ``planned`` entry from a *confirmed* uv-tool ownership report."""
    if operation not in OPERATIONS:
        raise ValueError(f"unknown lifecycle operation: {operation!r}")
    if not report.mutation_allowed:
        raise ValueError("a journal may only be created for a confirmed uv tool installation")
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return JournalEntry(
        operation=operation,
        phase="planned",
        started_at=stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        from_version=report.version,
        specifier=report.specifier,
        extras=report.extras,
        python_version=python_version,
        python_base=report.python_base,
        to_version=to_version,
        token=secrets.token_hex(16),
    )


def _refuse_if_secret(data: dict[str, Any]) -> None:
    """Never persist anything that looks like a credential."""
    for key, value in data.items():
        if key == "token":  # our own random operation id, never user or provider text
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, str) and redact_secrets(item) != item:
                raise ValueError(f"journal field {key!r} looks like a credential; not persisted")


def _stage(target: Path, data: dict[str, Any]) -> Path:
    """Write *data* to a fsynced temporary file next to *target*; return its path."""
    handle, tmp_name = tempfile.mkstemp(prefix=".journal-", suffix=".tmp", dir=target.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        _discard(tmp)
        raise
    return tmp


def _discard(tmp: Path) -> None:
    """Remove a staging file. Best effort: a leftover temp file is harmless, and cleanup
    must never turn an operation that already succeeded (or already failed) into a new error."""
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass


def _publish_exclusive(tmp: Path, target: Path) -> None:
    """Make the staged file appear at *target* only if nothing is there already.

    Raises :class:`FileExistsError` if *target* exists, whoever created it and however
    narrowly: the check and the creation are one filesystem operation, not two.

    * ``os.link`` fails with ``EEXIST`` instead of replacing, is atomic on POSIX and NTFS,
      and makes the complete, fsynced content appear at once.
    * Where hard links do not exist (some network and FAT-style filesystems) the fallback is
      ``O_CREAT | O_EXCL``: still exclusive, but the content is written after the file
      appears, so a crash in between leaves a partial file. :func:`read_journal` reports it
      as malformed and it blocks further operations, which is the safe direction.
    """
    try:
        os.link(tmp, target)
        return
    except FileExistsError:
        raise
    except (OSError, NotImplementedError):
        pass  # no hard links here: fall back to O_EXCL below
    payload = tmp.read_bytes()
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)  # FileExistsError if taken
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        _discard(target)  # we created it a moment ago and it is incomplete
        raise


def retained_error(path: Path, read: JournalRead | None = None) -> LifecycleError:
    """The REFUSED error for "a journal from an earlier operation is still there".

    Explains *why* nothing will run, what the journal says, and what a person can
    safely check, in terms of the operation that wrote it (an interrupted uninstall
    must not be described as a half-finished update). It never tells anyone to delete
    the file unconditionally, and no command inspects or clears it for them.
    """
    read = read or read_journal(path)
    entry = read.entry
    details: dict[str, Any] = {"reason": "journal_exists", "journal": str(path)}
    if entry is None:
        details["journal_state"] = read.state
        what = (
            f"The record of the earlier operation could not be read ({read.problem or read.state}), "
            "so what it was doing is unknown."
        )
    else:
        details |= {
            "journal_state": "ok",
            "operation": entry.operation,
            "phase": entry.phase,
            "started_at": entry.started_at,
            "from_version": entry.from_version,
            "to_version": entry.to_version,
        }
        span = f"{entry.from_version or '?'} -> {entry.to_version or '?'}"
        if entry.phase == "planned":
            what = (
                f"It was recorded at {entry.started_at} ({entry.operation}, {span}) but stopped "
                "before uv was started, so it should not have changed the installation."
            )
        else:
            state = {
                "update": "partly updated",
                "repair": "still damaged",
                "uninstall": "partly removed",
            }.get(entry.operation, "in an unknown state")
            what = (
                f"It was recorded at {entry.started_at} ({entry.operation}, {span}). uv may have "
                f"run, and the result was not confirmed, so the installation may be {state}."
            )
    versions = " or ".join(
        v for v in (entry and entry.from_version, entry and entry.to_version) if v
    )
    if entry is not None and entry.operation == "uninstall":
        recovery = (
            "Check before doing anything else: (1) no other Velune or uv process is running; "
            "(2) 'uv tool list' no longer shows velune-cli and 'velune' is no longer found. If "
            "both hold, the uninstall finished and the journal is stale: remove the file at "
            f"{path} yourself. If velune-cli is still listed or 'velune' still runs, run "
            "'uv tool uninstall velune-cli' in a fresh terminal, re-check, then remove the "
            "journal. Your data was not removed by uninstall. Do not delete the journal "
            "before checking."
        )
    else:
        recovery = (
            "Check before doing anything else: (1) no other Velune or uv process is running; "
            "(2) 'uv tool list' shows velune-cli"
            + (f" at {versions}" if versions else "")
            + ", and 'velune --version' runs. If both hold, the journal is stale: remove the "
            f"file at {path} yourself and run the command again. If velune-cli is missing, at "
            "another version, or does not run, reinstall it first with "
            "'uv tool install --force velune-cli' (add your extras if you use any), re-check, "
            "then remove the journal. Do not delete it before checking."
        )
    return LifecycleError(
        ExitCode.REFUSED,
        "An earlier lifecycle operation did not finish, so Velune will not start another one "
        f"until it is looked at. {what} Nothing was changed.",
        recovery=recovery,
        details=details,
    )


def write_journal(entry: JournalEntry, path: Path | None = None) -> Path:
    """Persist *entry*; refuse to replace anything already at *path*.

    Raises :class:`LifecycleError` (REFUSED) if a journal already exists or the
    path is not a regular file, :class:`LifecycleError` (BUSY) if another writer
    creates it first, and ``ValueError`` if a field looks like a secret.

    The early ``lexists`` check only produces the friendly "earlier operation did not
    finish" message. Exclusivity comes from :func:`_publish_exclusive`, so two writers
    that both pass the check still cannot both succeed.
    """
    target = path or default_journal_path()
    data = entry.to_json_dict()
    _refuse_if_secret(data)

    if os.path.lexists(target):
        raise retained_error(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    tmp = _stage(target, data)
    try:
        _publish_exclusive(tmp, target)
    except FileExistsError:  # lost the race: someone else's journal is in place, untouched
        raise LifecycleError(
            ExitCode.BUSY,
            "Another lifecycle operation holds the journal, so Velune did not start this one. "
            "Nothing was changed.",
            recovery=(
                "Lifecycle commands cannot run at the same time. Wait for the other one to "
                "finish, then try again. Do not delete the journal while it may be in use."
            ),
            details={"reason": "journal_race"},
        ) from None
    finally:
        _discard(tmp)
    return target


def _str_or_none(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise TypeError("expected a string")


def read_journal(path: Path | None = None) -> JournalRead:
    """Read a journal without ever raising on bad content."""
    target = path or default_journal_path()
    if not os.path.lexists(target):
        return JournalRead("missing")
    try:
        if target.is_symlink() or not target.is_file():
            return JournalRead("malformed", problem="journal path is not a regular file")
        if target.stat().st_size > _MAX_JOURNAL_BYTES:
            return JournalRead("malformed", problem="journal file is unreasonably large")
        raw = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return JournalRead("malformed", problem="journal file is unreadable")
    try:
        data = json.loads(raw)
    except ValueError:
        return JournalRead("malformed", problem="journal file is not valid JSON")
    if not isinstance(data, dict):
        return JournalRead("malformed", problem="journal is not a JSON object")
    if data.get("schema_version") != SCHEMA_VERSION:
        return JournalRead("malformed", problem="unsupported journal schema version")

    required = ("operation", "phase", "started_at", "from_version", "specifier", "extras")
    missing = [key for key in required if key not in data]
    if missing:
        return JournalRead("incomplete", problem="missing fields: " + ", ".join(missing))
    try:
        extras = data["extras"]
        if not isinstance(extras, list) or not all(isinstance(e, str) for e in extras):
            raise TypeError("extras must be a list of strings")
        if data["operation"] not in OPERATIONS or data["phase"] not in PHASES:
            raise TypeError("unknown operation or phase")
        started_at = data["started_at"]
        if not isinstance(started_at, str):
            raise TypeError("started_at must be a string")
        installer = data.get("installer", "uv")
        if not isinstance(installer, str):
            raise TypeError("installer must be a string")
        token = _str_or_none(data.get("token"))
        entry = JournalEntry(
            operation=data["operation"],
            phase=data["phase"],
            started_at=started_at,
            from_version=_str_or_none(data["from_version"]),
            specifier=_str_or_none(data["specifier"]),
            extras=tuple(extras),
            python_version=_str_or_none(data.get("python_version")),
            python_base=_str_or_none(data.get("python_base")),
            to_version=_str_or_none(data.get("to_version")),
            installer=installer,
            token=token,
        )
    except TypeError as exc:
        return JournalRead("malformed", problem=str(exc))
    return JournalRead("ok", entry=entry)


def _is_ours(path: Path, expected: JournalEntry) -> bool:
    """True only if the journal on disk is the one this operation wrote."""
    current = read_journal(path)
    if current.state != "ok" or current.entry is None:
        return False
    if expected.token is not None:  # the random id tells same-second operations apart
        return current.entry.token == expected.token
    return (current.entry.operation, current.entry.started_at, current.entry.from_version) == (
        expected.operation,
        expected.started_at,
        expected.from_version,
    )


def advance_phase(entry: JournalEntry, phase: str, path: Path | None = None) -> JournalEntry:
    """Move the journal *entry* wrote to *phase*. Refuses if the file is not that journal."""
    target = path or default_journal_path()
    if phase not in PHASES:
        raise ValueError(f"unknown journal phase: {phase!r}")
    if not _is_ours(target, entry):
        raise LifecycleError(
            ExitCode.REFUSED,
            "The lifecycle journal changed while the operation was starting, so Velune stopped "
            "before running anything. Nothing was changed.",
            recovery=(
                "Another Velune process may be running a lifecycle operation. Wait for it to "
                "finish, then try again. Do not delete the journal while it may be in use."
            ),
            details={"reason": "journal_not_ours"},
        )
    updated = replace(entry, phase=phase)
    tmp = _stage(target, updated.to_json_dict())
    try:
        os.replace(tmp, target)
    finally:
        _discard(tmp)
    return updated


def clear_journal(entry: JournalEntry, path: Path | None = None) -> bool:
    """Delete the journal if (and only if) it is the one *entry* wrote. True if removed."""
    target = path or default_journal_path()
    if not _is_ours(target, entry):
        return False
    try:
        target.unlink()
    except OSError:
        return False
    return True
