"""Bounded, non-blocking workspace search tools (``grep_files``, ``find_files``).

Every search here is bounded in work, time and output, and none of it runs on the
interactive event loop:

* File discovery and ``find_files`` run in a worker thread that checks a cancel
  flag and a deadline between directories/files.
* Regex matching runs in a short-lived **isolated process** (``_grep_worker.py``).
  CPython's ``re`` holds the GIL for an entire ``search`` call and cannot be
  interrupted, so a catastrophic-backtracking pattern would freeze the UI from a
  thread; a process can simply be killed at the deadline.

When a bound is hit the result list ends with a deterministic marker,
``{"truncated": True, "reasons": [...], ...}``, so a caller (or the model) is never
handed incomplete results as if they were complete.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from velune.execution.path_guard import resolve_in_workspace
from velune.tools.base.tool import BaseTool, ToolPermission
from velune.tools.filesystem.ignore import load_ignore

# ── Documented limits ────────────────────────────────────────────────────────
#: Matching lines returned by one ``grep_files`` call.
GREP_MAX_MATCHES = 100
#: Files searched by one ``grep_files`` call (deterministic, sorted order).
GREP_MAX_FILES = 5_000
#: Files larger than this are skipped (and reported), not read.
GREP_MAX_FILE_BYTES = 1_048_576
#: A line is clipped to this many characters before matching.
GREP_MAX_LINE_CHARS = 2_000
#: Characters of a matching line / matched text returned per hit.
GREP_TEXT_CHARS = 200
#: Approximate cap on the characters of one ``grep_files`` result.
GREP_MAX_OUTPUT_CHARS = 12_000
#: Wall-clock budget for one ``grep_files`` call, file discovery included.
GREP_TIME_BUDGET_SECONDS = 10.0

#: Paths returned by one ``find_files`` call.
FIND_MAX_RESULTS = 500
#: Directory entries examined by one ``find_files`` call.
FIND_MAX_ENTRIES = 100_000
#: Wall-clock budget for one ``find_files`` call.
FIND_TIME_BUDGET_SECONDS = 10.0

_WORKER = Path(__file__).with_name("_grep_worker.py")


def truncation_marker(reasons: list[str], **extra: Any) -> dict[str, Any]:
    """The trailing list element that tells a reader the results are incomplete."""
    return {"truncated": True, "reasons": reasons, **extra}


class GrepFiles(BaseTool):
    """Search file contents with a case-insensitive regular expression.

    Semantics are those of ``grep``: the pattern is applied to each line
    separately and at most one hit is returned per matching line.
    """

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()
        self._ignore = load_ignore(self.workspace)

    def get_required_permissions(self) -> set[ToolPermission]:
        # Read-only: runs in every execution mode without approval.
        return {ToolPermission.FILESYSTEM_READ}

    def get_name(self) -> str:
        return "grep_files"

    def get_description(self) -> str:
        return (
            "Search for a regular expression in files (per line, case-insensitive). "
            f"Returns at most {GREP_MAX_MATCHES} matches; a trailing {{'truncated': true}} "
            "entry means the results are incomplete -- narrow the pattern or directory."
        )

    async def execute(
        self,
        pattern: str,
        directory: str = ".",
        file_pattern: str = "*",
    ) -> list[dict]:
        """Search for *pattern* in files under *directory*."""
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"Invalid regular expression {pattern!r}: {exc}") from exc

        root_path = resolve_in_workspace(directory, self.workspace, label="grep_files")
        deadline = time.monotonic() + GREP_TIME_BUDGET_SECONDS

        cancel = threading.Event()
        try:
            files, discovery_reasons = await asyncio.to_thread(
                self._collect_files, root_path, file_pattern, deadline, cancel
            )
        except asyncio.CancelledError:
            cancel.set()
            raise

        reasons = list(discovery_reasons)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return [truncation_marker([*reasons, "time_budget"], files_searched=0)]

        matches, report = await self._scan_isolated(pattern, files, remaining)

        if report.get("stop_reason") == "max_matches":
            reasons.append(f"max_matches={GREP_MAX_MATCHES}")
        elif report.get("stop_reason") == "output_limit":
            reasons.append(f"output_limit~{GREP_MAX_OUTPUT_CHARS}_chars")
        if report.get("timed_out"):
            reasons.append(f"time_budget={GREP_TIME_BUDGET_SECONDS:g}s")
        if report.get("skipped_large"):
            reasons.append(f"skipped_files_over_{GREP_MAX_FILE_BYTES}_bytes")
        if report.get("worker_failed"):
            reasons.append("search_worker_failed")

        results: list[dict[str, Any]] = list(matches)
        if reasons:
            extra = {
                k: report[k]
                for k in ("files_scanned", "skipped_large", "skipped_binary", "skipped_unreadable")
                if k in report
            }
            results.append(truncation_marker(reasons, **extra))
        return results

    # ── file discovery (thread) ──────────────────────────────────────────────

    def _collect_files(
        self,
        root: Path,
        file_pattern: str,
        deadline: float,
        cancel: threading.Event,
    ) -> tuple[list[str], list[str]]:
        from velune.repository.scanner import FilesystemScanner

        extensions = None
        if file_pattern and file_pattern != "*":
            if file_pattern.startswith("*.") and not any(
                c in file_pattern[2:] for c in ["*", "?", "[", "]"]
            ):
                extensions = [file_pattern[1:]]

        scanner = FilesystemScanner(root)
        candidates = scanner.scan(extensions)
        reasons: list[str] = []
        files: list[str] = []
        for file_path in candidates:
            if cancel.is_set():
                break
            if time.monotonic() > deadline:
                reasons.append(f"time_budget={GREP_TIME_BUDGET_SECONDS:g}s")
                break
            if self._ignore.is_ignored(file_path):
                continue
            if file_pattern and not file_path.match(file_pattern):
                continue
            files.append(str(file_path))
            if len(files) >= GREP_MAX_FILES:
                reasons.append(f"file_limit={GREP_MAX_FILES}")
                break
        return files, reasons

    # ── matching (isolated process) ──────────────────────────────────────────

    async def _scan_isolated(
        self, pattern: str, files: list[str], budget: float
    ) -> tuple[list[dict], dict[str, Any]]:
        request = json.dumps(
            {
                "pattern": pattern,
                "files": files,
                "max_matches": GREP_MAX_MATCHES,
                "max_file_bytes": GREP_MAX_FILE_BYTES,
                "max_line_chars": GREP_MAX_LINE_CHARS,
                "text_chars": GREP_TEXT_CHARS,
                "max_output_chars": GREP_MAX_OUTPUT_CHARS,
            }
        )
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = await asyncio.to_thread(
                subprocess.Popen,
                [sys.executable, "-I", str(_WORKER)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=str(self.workspace),
                creationflags=flags,
            )
        except OSError:
            return [], {"worker_failed": True}

        def _communicate() -> tuple[bytes, bool]:
            try:
                out, _ = proc.communicate(request.encode("utf-8"), timeout=budget)
                return out or b"", False
            except subprocess.TimeoutExpired:
                proc.kill()
                out, _ = proc.communicate()
                return out or b"", True

        try:
            raw, timed_out = await asyncio.to_thread(_communicate)
        except asyncio.CancelledError:
            proc.kill()  # unblocks the waiting thread; nothing outlives the cancel
            raise
        finally:
            if proc.poll() is None:
                proc.kill()

        matches: list[dict] = []
        report: dict[str, Any] = {"timed_out": timed_out}
        finished = False
        for line in raw.decode("utf-8", errors="replace").splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue  # a line cut off by the kill
            if "m" in obj:
                matches.append(obj["m"])
            elif "done" in obj:
                report.update(obj["done"])
                finished = True
            elif "error" in obj:
                raise ValueError(obj["error"])
        if not finished and not timed_out:
            report["worker_failed"] = True
        return matches, report

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regular expression to search for (applied per line)",
                },
                "directory": {
                    "type": "string",
                    "description": "Directory to search in (relative to the workspace)",
                },
                "file_pattern": {
                    "type": "string",
                    "description": "File pattern to match, e.g. '*.py'",
                },
            },
            "required": ["pattern"],
        }


class FindFiles(BaseTool):
    """Find files by name pattern, pruning ignored directories instead of walking them."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()
        self._ignore = load_ignore(self.workspace)

    def get_required_permissions(self) -> set[ToolPermission]:
        # Read-only: runs in every execution mode without approval.
        return {ToolPermission.FILESYSTEM_READ}

    def get_name(self) -> str:
        return "find_files"

    def get_description(self) -> str:
        return (
            "Find files by name pattern. "
            f"Returns at most {FIND_MAX_RESULTS} paths; a trailing {{'truncated': true}} "
            "entry means the results are incomplete."
        )

    async def execute(
        self,
        pattern: str,
        directory: str = ".",
    ) -> list:
        """Find files whose name matches the glob *pattern*."""
        root_path = resolve_in_workspace(directory, self.workspace, label="find_files")
        cancel = threading.Event()
        try:
            return await asyncio.to_thread(self._walk, root_path, pattern, cancel)
        except asyncio.CancelledError:
            cancel.set()
            raise

    def _walk(self, root: Path, pattern: str, cancel: threading.Event) -> list:
        deadline = time.monotonic() + FIND_TIME_BUDGET_SECONDS
        matches: list[str] = []
        reasons: list[str] = []
        entries = 0

        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            if cancel.is_set():
                break
            if time.monotonic() > deadline:
                reasons.append(f"time_budget={FIND_TIME_BUDGET_SECONDS:g}s")
                break
            here = Path(dirpath)
            # Prune ignored directories in place so os.walk never descends into
            # node_modules / .venv / build output. A directory is ignored when a
            # child of it would be (this is how ``dir/`` patterns match).
            dirnames[:] = sorted(d for d in dirnames if not self._ignore.is_ignored(here / d / "_"))
            for name in sorted(filenames):
                entries += 1
                if not fnmatch.fnmatch(name, pattern):
                    continue
                path = here / name
                if self._ignore.is_ignored(path):
                    continue
                matches.append(str(path))
                if len(matches) >= FIND_MAX_RESULTS:
                    reasons.append(f"max_results={FIND_MAX_RESULTS}")
                    break
            entries += len(dirnames)
            if reasons:
                break
            if entries >= FIND_MAX_ENTRIES:
                reasons.append(f"entry_limit={FIND_MAX_ENTRIES}")
                break

        if reasons:
            return [*matches, truncation_marker(reasons, entries_examined=entries)]
        return matches

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "File name glob to match, e.g. '*.py'",
                },
                "directory": {
                    "type": "string",
                    "description": "Directory to search in (relative to the workspace)",
                },
            },
            "required": ["pattern"],
        }
