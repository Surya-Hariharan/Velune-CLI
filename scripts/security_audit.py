"""Static security audit checks for the Velune codebase.

Runs a set of pattern-level checks that CI enforces to prevent security
regressions. Each check function returns a list of (file, line, message)
findings; an empty list means the check passed.

Exit codes:
  0 — all checks passed
  1 — one or more checks failed
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Allowlists — entries here are exempt from the corresponding check.
# Each entry is a (file_suffix, pattern) pair where file_suffix is matched
# against the end of the relative file path and pattern is a substring that
# must appear on the offending line.
# ---------------------------------------------------------------------------

# velune/kernel/entrypoint.py holds the single legitimate asyncio.run() call
# and the single legitimate asyncio.get_running_loop() guard call.
_SYNC_OVER_ASYNC_ALLOWLIST: list[tuple[str, str]] = [
    # The single authorised asyncio.run() in the process entry point.
    ("velune/kernel/entrypoint.py", "asyncio.run("),
    # The guard that checks we are NOT already in a running loop.
    ("velune/kernel/entrypoint.py", "asyncio.get_running_loop()"),
]

# velune/plugins/runner.py is a subprocess worker: it is spawned as an isolated
# child process by PluginSandbox and has no inherited event loop.  Using
# asyncio.run() there is architecturally correct and intentional.
_ASYNCIO_RUN_ALLOWLIST: set[str] = {
    "velune/kernel/entrypoint.py",
    "velune/plugins/runner.py",
}

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Check helpers
# ---------------------------------------------------------------------------

Finding = tuple[str, int, str]  # (relative_path, line_number, message)


def _iter_py_files(root: Path, package: str) -> list[Path]:
    return sorted((root / package).rglob("*.py"))


def _relative(path: Path) -> str:
    return path.relative_to(_REPO_ROOT).as_posix()


def _allowlisted(rel: str, line_text: str, allowlist: list[tuple[str, str]]) -> bool:
    for suffix, substr in allowlist:
        if rel.endswith(suffix) and substr in line_text:
            return True
    return False


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_no_sync_over_async(root: Path = _REPO_ROOT, package: str = "velune") -> list[Finding]:
    """Fail if run_until_complete() or get_event_loop() appear outside the allowlist.

    Both patterns indicate sync-over-async bridging that bypasses the single
    managed event loop in velune/kernel/entrypoint.py and can cause deadlocks
    or hidden concurrency bugs in async contexts.
    """
    findings: list[Finding] = []
    patterns = [
        (re.compile(r"\.run_until_complete\("), "run_until_complete() — use run_async() from velune.kernel.entrypoint"),
        (re.compile(r"asyncio\.get_event_loop\(\)"), "asyncio.get_event_loop() — await directly or use run_async()"),
    ]

    for py_file in _iter_py_files(root, package):
        rel = _relative(py_file)
        for lineno, raw in enumerate(py_file.read_text(encoding="utf-8").splitlines(), start=1):
            for pattern, message in patterns:
                if pattern.search(raw) and not _allowlisted(rel, raw, _SYNC_OVER_ASYNC_ALLOWLIST):
                    findings.append((rel, lineno, message))

    return findings


def check_no_shell_true(root: Path = _REPO_ROOT, package: str = "velune") -> list[Finding]:
    """Fail if shell=True appears anywhere in the package (P0-2 regression guard)."""
    findings: list[Finding] = []
    pattern = re.compile(r"\bshell\s*=\s*True\b")

    for py_file in _iter_py_files(root, package):
        rel = _relative(py_file)
        for lineno, raw in enumerate(py_file.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(raw):
                findings.append((rel, lineno, "shell=True in subprocess call (P0-2 regression)"))

    return findings


def check_asyncio_run_count(root: Path = _REPO_ROOT, package: str = "velune") -> list[Finding]:
    """Fail if asyncio.run() appears outside the allowlist (P0-1 regression guard).

    Allowed locations are listed in _ASYNCIO_RUN_ALLOWLIST.  Any new call site
    that is architecturally justified (e.g. a standalone subprocess worker) must
    be added to that set with an explanatory comment.
    """
    findings: list[Finding] = []
    pattern = re.compile(r"\basyncio\.run\(")

    for py_file in _iter_py_files(root, package):
        rel = _relative(py_file)
        # Strip any leading package prefix so both posix/windows relative paths match
        rel_norm = rel.replace("\\", "/")
        if rel_norm in _ASYNCIO_RUN_ALLOWLIST:
            continue
        for lineno, raw in enumerate(py_file.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(raw):
                findings.append(
                    (
                        rel,
                        lineno,
                        "asyncio.run() outside allowlist — add to _ASYNCIO_RUN_ALLOWLIST "
                        "with justification if intentional (P0-1 regression guard).",
                    )
                )

    return findings


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_CHECKS = [
    ("check_no_sync_over_async", check_no_sync_over_async),
    ("check_no_shell_true", check_no_shell_true),
    ("check_asyncio_run_count", check_asyncio_run_count),
]


def main() -> int:
    all_passed = True

    for name, check_fn in _CHECKS:
        findings = check_fn()
        if findings:
            all_passed = False
            print(f"\nFAIL: {name} ({len(findings)} finding(s)):")
            for rel, lineno, msg in findings:
                print(f"   {rel}:{lineno}: {msg}")
        else:
            print(f"PASS: {name}")

    if all_passed:
        print("\nAll security audit checks passed.")
        return 0

    print("\nSecurity audit FAILED -- see findings above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
