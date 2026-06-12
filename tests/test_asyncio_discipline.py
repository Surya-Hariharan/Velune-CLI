"""Enforce asyncio.run() appears only in velune/kernel/entrypoint.py.

Rationale: scattered asyncio.run() calls cause "event loop already running"
errors when async contexts call into other async contexts.  A single
centralised run_async() wrapper prevents the class of bugs.
"""

from __future__ import annotations

import re
from pathlib import Path

# Negative lookbehind for `` ` `` skips RST/docstring annotations like
# ``asyncio.run()`` while still catching real call expressions.
_PATTERN = re.compile(r"(?<!`)\basyncio\.run\s*\(")

# The one file allowed to contain asyncio.run().
_ALLOWLIST = frozenset(["velune/kernel/entrypoint.py"])


def test_asyncio_run_only_in_entrypoint() -> None:
    """Fail if any velune source file outside the allowlist calls asyncio.run()."""
    root = Path(__file__).parent.parent
    violations: list[str] = []

    for pyfile in sorted(root.glob("velune/**/*.py")):
        rel = pyfile.relative_to(root).as_posix()
        if rel in _ALLOWLIST:
            continue
        try:
            text = pyfile.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _PATTERN.search(text):
            violations.append(rel)

    assert not violations, (
        "asyncio.run() found outside the designated entrypoint "
        "(velune/kernel/entrypoint.py):\n" + "\n".join(f"  {v}" for v in violations)
    )
