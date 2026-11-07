"""Read-only check that an installed distribution still matches what was installed.

Why this exists (P2.3.1)
------------------------
A version number is not proof that an installation works, in either direction.
On Linux (WSL2, uv 0.12.3, synthetic package) stopping ``uv tool upgrade`` part
way through its file copy left the *new* ``dist-info`` beside missing package
files. Ownership detection caught every such case, but only because uv wrote
``INSTALLER`` last: an ordering that was observed, not a uv guarantee. P2.0 also
saw uv exit 0 on a damaged Windows environment. This check does not depend on
that ordering, and it supplies the evidence the "version unchanged" outcome
needs (an unchanged version does not show that uv left the files alone).

The evidence is the ``RECORD`` file every wheel install writes: each listed file
must exist and match its recorded size and SHA-256. That is the installer's own
statement of what it put on disk. Checked against uv-installed black (compiled
extension), ruff (binary), httpie, pytest and glances: no false positives.

What this does **not** prove: it covers one distribution (``velune-cli``), not
its dependencies, and it does not run the code. ``distributions`` (name and
version of everything importable) is returned so a caller can at least notice
that *something* else changed. Nothing here writes, repairs or deletes.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.metadata as importlib_metadata
import io
import os
from dataclasses import dataclass
from pathlib import Path

from velune.lifecycle.ownership import DISTRIBUTION, normalize_name

_MAX_FILES = 50_000
_MAX_BYTES = 512 * 1024 * 1024
_MAX_NAMED = 3

INTACT = "intact"
DAMAGED = "damaged"
UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class Integrity:
    """``record`` is intact | damaged | unverifiable; ``problems`` never contain full paths."""

    record: str
    problems: tuple[str, ...] = ()
    checked: int = 0
    distributions: tuple[tuple[str, str], ...] | None = None  # None: could not be listed

    @property
    def intact(self) -> bool:
        return self.record == INTACT


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return base64.urlsafe_b64encode(sha.digest()).rstrip(b"=").decode("ascii")


def list_distributions() -> tuple[tuple[str, str], ...] | None:
    """Sorted ``(normalised name, version)`` of every importable distribution, or None."""
    try:
        found = {
            (normalize_name(str(dist.metadata["Name"])), str(dist.version))
            for dist in importlib_metadata.distributions()
            if dist.metadata["Name"]
        }
    except Exception:
        return None
    return tuple(sorted(found))


def verify_record(distribution: str = DISTRIBUTION) -> Integrity:
    """Compare *distribution*'s installed files with its ``RECORD``. Never raises."""
    try:
        dist = importlib_metadata.distribution(distribution)
        text = dist.read_text("RECORD")
        base = Path(str(dist.locate_file("")))
    except Exception:
        return Integrity(UNVERIFIABLE, ("the package metadata could not be read",))
    if not text:
        return Integrity(UNVERIFIABLE, ("the package has no RECORD file",))

    missing: list[str] = []
    changed: list[str] = []
    checked = 0
    total = 0
    try:
        for row in csv.reader(io.StringIO(text)):
            if len(row) != 3 or not row[1]:
                continue  # RECORD itself, bytecode: not hashed by design
            name, spec, size = row
            algorithm, _, expected = spec.partition("=")
            if algorithm != "sha256" or not expected:
                continue
            checked += 1
            if checked > _MAX_FILES:
                return Integrity(UNVERIFIABLE, ("the package lists too many files to check",))
            target = Path(os.path.normpath(base / name))
            if not target.is_file():
                missing.append(name)
                continue
            total += target.stat().st_size
            if total > _MAX_BYTES:
                return Integrity(UNVERIFIABLE, ("the package is too large to check",), checked)
            if (size.isdigit() and target.stat().st_size != int(size)) or _digest(
                target
            ) != expected:
                changed.append(name)
    except Exception:
        return Integrity(UNVERIFIABLE, ("the installed files could not be read",), checked)

    if checked == 0:
        return Integrity(UNVERIFIABLE, ("the RECORD file lists no checkable files",))
    problems: list[str] = []
    if missing:
        problems.append(f"{len(missing)} file(s) missing (e.g. {', '.join(missing[:_MAX_NAMED])})")
    if changed:
        problems.append(f"{len(changed)} file(s) changed (e.g. {', '.join(changed[:_MAX_NAMED])})")
    return Integrity(DAMAGED if problems else INTACT, tuple(problems), checked)


def check_installation(distribution: str = DISTRIBUTION) -> Integrity:
    """``verify_record`` plus the list of installed distributions, for before/after comparison."""
    record = verify_record(distribution)
    return Integrity(record.record, record.problems, record.checked, list_distributions())
