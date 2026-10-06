"""Plan-mode state: the active plan file, its status, and its execution scope.

A plan lives at ``<workspace>/.velune/plans/<name>.md``. Its ``Files Affected``
table is the execution scope once approved: the policy then allows changes to
those files and asks before anything else (a scope change).

Status line (in the plan's ``## Approval`` section)::

    WAITING_FOR_USER_APPROVAL → EXECUTING → DONE
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from velune.permissions.policy import ExecutionMode, PolicyState

WAITING = "WAITING_FOR_USER_APPROVAL"
EXECUTING = "EXECUTING"
DONE = "DONE"

_STATUS_RE = re.compile(r"^(\s*Status:\s*)(\S+)", re.MULTILINE)

# Only an explicit go-ahead starts execution. "looks good" / "okay" / "I see"
# are acknowledgements, not approval; a message that approves *and* asks for
# changes is treated as a revision.
_APPROVAL_RE = re.compile(
    r"^\s*(yes[,!.]?\s+)?("
    r"approve(d)?|i approve|approve (the |this )?plan|plan approved|"
    r"proceed|yes proceed|go ahead|go for it|execute( it| the plan| this plan)?|"
    r"implement( it| the plan| this plan)?|run (it|the plan)|do it|ship it|start( execution)?"
    r")\s*[.!]*\s*(please\s*[.!]*)?\s*$",
    re.IGNORECASE,
)

PLAN_TEMPLATE = """# Plan

## Objective
## Current State
## Root Cause
## Proposed Changes
### 1. File changes
### 2. Code changes
### 3. Configuration changes
### 4. Dependencies
### 5. Tests
### 6. Validation
## Files Affected
| Action | File |
|---|---|
| Modify | path/to/file |
## Risks
## Rollback
## Execution Order
1.
## Approval
Status: WAITING_FOR_USER_APPROVAL
"""


def is_approval(text: str) -> bool:
    """True only for an explicit instruction to execute the plan."""
    return bool(_APPROVAL_RE.match(text.strip()))


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (slug or "plan")[:60]


def parse_status(text: str) -> str | None:
    match = _STATUS_RE.search(text)
    return match.group(2).strip() if match else None


def set_status(text: str, status: str) -> str:
    if _STATUS_RE.search(text):
        return _STATUS_RE.sub(lambda m: m.group(1) + status, text, count=1)
    return text.rstrip() + f"\n\n## Approval\n\nStatus: {status}\n"


def parse_files_affected(text: str) -> list[tuple[str, str]]:
    """``(action, path)`` rows from the plan's ``Files Affected`` table."""
    section = re.search(r"^##\s*Files Affected\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S | re.I)
    if not section:
        return []
    rows: list[tuple[str, str]] = []
    for line in section.group(1).splitlines():
        cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or not cells[0] or set(cells[0]) <= set("-: "):
            continue
        action, path = cells[0], cells[1]
        if action.lower() == "action" or not path or path.lower() in {"file", "path/to/file"}:
            continue
        rows.append((action.capitalize(), path))
    return rows


@dataclass
class PlanManager:
    workspace: Path
    active: Path | None = None
    status: str | None = None
    files: list[tuple[str, str]] = field(default_factory=list)
    turn_started: float = 0.0

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).resolve()

    @property
    def plans_dir(self) -> Path:
        return self.workspace / ".velune" / "plans"

    @property
    def waiting(self) -> bool:
        return self.active is not None and self.status == WAITING

    @property
    def executing(self) -> bool:
        return self.active is not None and self.status == EXECUTING

    def scope(self) -> frozenset[Path]:
        paths = set()
        for _, raw in self.files:
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = self.workspace / candidate
            paths.add(candidate.resolve())
        return frozenset(paths)

    def apply_to(self, state: PolicyState) -> None:
        """Point the policy at the plans dir and (while executing) the plan's scope."""
        state.plans_dir = self.plans_dir
        state.plan_executing = self.executing and state.mode is ExecutionMode.PLAN
        state.plan_scope = self.scope() if state.plan_executing else frozenset()

    # ── lifecycle ────────────────────────────────────────────────────────

    def begin_turn(self) -> None:
        self.turn_started = time.time()

    def newest_written_this_turn(self) -> Path | None:
        if not self.plans_dir.is_dir():
            return None
        fresh = [
            p for p in self.plans_dir.glob("*.md") if p.stat().st_mtime >= self.turn_started - 1.0
        ]
        return max(fresh, key=lambda p: p.stat().st_mtime) if fresh else None

    def record(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        self.active = path
        self.status = parse_status(text) or WAITING
        if self.status not in {WAITING, EXECUTING, DONE}:
            self.status = WAITING
        self.files = parse_files_affected(text)

    def record_new(self, path: Path) -> None:
        """Track a plan written this turn. Whatever Status it claims, it is unapproved."""
        text = path.read_text(encoding="utf-8")
        if parse_status(text) != WAITING:
            path.write_text(set_status(text, WAITING), encoding="utf-8")
        self.record(path)

    def suspend(self) -> bool:
        """Pause an executing plan: it needs a fresh approval to run again.

        Called when the user leaves PLAN mode mid-execution, so a stale
        EXECUTING status can never keep (or later regain) write access.
        """
        if not self.executing:
            return False
        assert self.active is not None
        text = self.active.read_text(encoding="utf-8")
        self.active.write_text(set_status(text, WAITING), encoding="utf-8")
        self.record(self.active)
        return True

    def state_label(self) -> str | None:
        """Short phrase for the status bar, or None when no plan is pending."""
        if self.waiting:
            return "awaiting approval"
        if self.executing:
            return "executing"
        return None

    def approve(self) -> None:
        assert self.active is not None
        text = self.active.read_text(encoding="utf-8")
        self.active.write_text(set_status(text, EXECUTING), encoding="utf-8")
        self.record(self.active)

    def finish(self, report: str) -> None:
        assert self.active is not None
        text = set_status(self.active.read_text(encoding="utf-8"), DONE)
        stamp = time.strftime("%Y-%m-%d %H:%M")
        text = text.rstrip() + f"\n\n## Results ({stamp})\n\n{report.strip() or '(no report)'}\n"
        self.active.write_text(text, encoding="utf-8")
        self.record(self.active)

    def summary(self) -> dict[str, int]:
        counts = {"modify": 0, "create": 0, "delete": 0, "tests": 0}
        for action, path in self.files:
            key = action.lower()
            if key in ("add", "create", "new"):
                counts["create"] += 1
            elif key in ("delete", "remove"):
                counts["delete"] += 1
            else:
                counts["modify"] += 1
            if "test" in path.lower():
                counts["tests"] += 1
        return counts
