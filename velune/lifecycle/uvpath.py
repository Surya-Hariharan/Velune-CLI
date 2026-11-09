"""Find the ``uv`` executable without ever running something planted in the current directory.

``shutil.which`` is unsafe here: on Windows with Python 3.10/3.11 it searches the
current directory first and tries ``PATHEXT`` (``.bat``, ``.cmd``), and a ``.bat``
file is executed through ``cmd.exe``. A repository containing a ``uv.bat`` would
then run when the user typed ``velune update --check``. So lifecycle code never
uses ``shutil.which`` for uv. This module walks ``PATH`` itself, and the result does
not depend on the Python version.

Rules (fail closed):

* A PATH entry must be absolute, free of ``..``, and neither the current directory
  nor below it. Empty and ``.`` entries are skipped.
* Windows accepts ``uv.exe`` only (never ``.bat``/``.cmd``/``.com``, which run through
  a shell interpreter). POSIX accepts a regular file named ``uv`` with the execute bit.
* The file's real path must also be outside the current directory.
* Filesystem errors (an unreadable or deleted cwd, a NUL byte, an unresolvable path)
  never escape: they end in a refusal. A directory whose identity is unreliable
  (inode 0) counts as unknown, which also refuses.
* The result is an absolute path. Callers never receive a bare ``uv``.

Standard library only: lifecycle commands must work in a damaged environment.
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

NOT_FOUND = "not_found"
UNTRUSTED = "only_untrusted_location"
CWD_UNAVAILABLE = "cwd_unavailable"


@dataclass(frozen=True)
class UvLookup:
    path: str | None
    reason: str | None  # None when found; NOT_FOUND, UNTRUSTED or CWD_UNAVAILABLE otherwise


def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p))


def _at_or_below(candidate: str, base: str) -> bool:
    try:
        return os.path.commonpath([candidate, base]) == base
    except ValueError:  # different drives
        return False


def _entry_is_safe(entry: str, cwd: str) -> bool:
    if not entry or entry == ".":
        return False
    if not os.path.isabs(entry):
        return False
    if ".." in Path(entry).parts:
        return False
    return not _at_or_below(_norm(entry), cwd)


def _is_executable_file(path: str, windows: bool) -> bool:
    # OSError: missing/denied/not a directory. ValueError: an embedded NUL byte.
    try:
        st = os.stat(path)  # follows links on purpose: a link to a real file is fine
        if not stat.S_ISREG(st.st_mode):
            return False
        return True if windows else os.access(path, os.X_OK)
    except (OSError, ValueError):
        return False


def _real(path: str) -> str | None:
    """``realpath`` or ``None`` when it cannot be resolved (callers then refuse)."""
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return None


def find_uv(
    *,
    path_env: str | None = None,
    cwd: str | None = None,
    platform: str | None = None,
    pathsep: str | None = None,
) -> UvLookup:
    """Return the first trusted ``uv`` on PATH, or why none could be trusted."""
    plat = platform or sys.platform
    windows = plat.startswith("win")
    try:
        cwd_for_identity = cwd if cwd is not None else os.getcwd()
        base = _norm(cwd_for_identity)
    except (OSError, ValueError):
        # E.g. the directory was deleted under us. Without a cwd nothing can be proven
        # to be outside it, so fail closed.
        return UvLookup(None, CWD_UNAVAILABLE)
    raw = path_env if path_env is not None else os.environ.get("PATH", "")
    names = ("uv.exe",) if windows else ("uv",)

    saw_untrusted = False
    for entry in raw.split(pathsep or (";" if windows else ":")):
        entry = entry.strip().strip('"')
        # An unsafe entry is only worth reporting if it really holds a uv.
        safe = _entry_is_safe(entry, base)
        for name in names:
            candidate = os.path.join(entry, name) if entry else name
            if not safe:
                if entry and entry != "." and _looks_like_uv(entry, windows):
                    saw_untrusted = True
                continue
            if not _is_executable_file(candidate, windows):
                continue
            resolved = _real(candidate)
            if resolved is None:  # cannot establish where it really lives: refuse
                saw_untrusted = True
                continue
            if _at_or_below(_norm(resolved), base):
                saw_untrusted = True
                continue
            # String checks cannot see alias spellings of the cwd (extended-length or UNC/admin-share);
            # compare filesystem identity too. True or None (unknown) both refuse.
            if _in_cwd_by_identity(candidate, resolved, cwd_for_identity) is not False:
                saw_untrusted = True
                continue
            return UvLookup(os.path.abspath(candidate), None)
    # The classic plant: a launcher in the cwd that PATH never mentioned.
    saw_untrusted = saw_untrusted or _looks_like_uv(base, windows)
    return UvLookup(None, UNTRUSTED if saw_untrusted else NOT_FOUND)


def _same_directory(a: os.stat_result, b: os.stat_result) -> bool | None:
    """Whether two stat results are one directory; ``None`` when that cannot be told.

    ``os.path.samestat`` compares only ``(st_ino, st_dev)``. Some filesystems report
    ``st_ino == 0`` for everything, which would make unrelated directories on one device
    compare equal. Different devices are a definite "no"; on the same device an inode of
    zero means the identity is unknown, never "same" and never "different".
    """
    if a.st_dev != b.st_dev:
        return False
    if a.st_ino == 0 or b.st_ino == 0:
        return None
    return os.path.samestat(a, b)


def _in_cwd_by_identity(candidate: str, real: str, cwd: str) -> bool | None:
    """True if *candidate* is in the cwd (or below it) by filesystem identity, else False.

    Walks every ancestor directory of the candidate's own spelling and of its resolved
    path and compares each with the cwd using ``os.path.samestat``, so every spelling of
    one directory (extended-length, UNC/admin share, short names, junctions, symlinks)
    gives the same answer. Links to a safe place are fine: only identity matters. Returns
    ``None`` when identity cannot be established (callers treat that as a refusal).
    """
    try:
        cwd_stat = os.stat(cwd)
    except (OSError, ValueError):
        return None
    for start in (candidate, real):
        current = os.path.dirname(start)
        while True:
            try:
                same = _same_directory(os.stat(current), cwd_stat)
            except (OSError, ValueError):
                return None
            if same is None:  # identity unreliable on this filesystem: unknown, so refuse
                return None
            if same:
                return True
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    return False


def _looks_like_uv(entry: str, windows: bool) -> bool:
    try:
        return any(
            os.path.lexists(os.path.join(entry, n))
            for n in (("uv.exe", "uv.bat", "uv.cmd", "uv.com") if windows else ("uv",))
        )
    except (OSError, ValueError):
        return False


def trusted_which(name: str) -> str | None:
    """Drop-in for ``shutil.which`` that only ever resolves ``uv``, and only safely."""
    if name != "uv":
        return None
    return find_uv().path


def uv_problem_message(reason: str | None) -> str:
    """Sanitized explanation (no paths) for a failed lookup."""
    if reason == UNTRUSTED:
        return (
            "uv was found only in an untrusted location (the current directory, a directory "
            "below it, or a relative PATH entry), so it was not run. This is a security "
            "restriction, not a broken uv: run the command again from a directory that does "
            "not contain uv"
        )
    if reason == CWD_UNAVAILABLE:
        return (
            "the current directory could not be inspected (was it deleted?), so uv was not "
            "run: run the command again from an existing directory"
        )
    return "uv was not found on PATH"
