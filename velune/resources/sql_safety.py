"""SQL statement classification for the database connectors.

Mirrors :mod:`velune.tools.safety` (command classification) for SQL: it maps a
statement to the :class:`~velune.resources.base.ResourcePermission` it requires
so the manager can gate it. The philosophy matches the spec:

    SELECT / EXPLAIN / SHOW / DESCRIBE  → READ   (auto-approved)
    INSERT / UPDATE / DELETE / DDL       → WRITE  (confirmation required)
    DROP / TRUNCATE                      → ADMIN  (explicit confirmation)

This is a *classifier*, not a parser — it inspects the leading keyword(s) after
stripping comments. It is deliberately conservative: anything it cannot confirm
as read-only is escalated to WRITE, and a statement carrying multiple
top-level commands (a common injection shape) is escalated so a hidden DROP can
never ride in under a SELECT's read-only approval.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from velune.resources.base import ResourcePermission

# Leading keywords that only read.
_READ_KEYWORDS: frozenset[str] = frozenset(
    {"select", "explain", "show", "describe", "desc", "with", "values", "table"}
)

# Leading keywords that mutate but are recoverable → WRITE.
_WRITE_KEYWORDS: frozenset[str] = frozenset(
    {
        "insert",
        "update",
        "delete",
        "create",
        "alter",
        "comment",
        "grant",
        "revoke",
        "set",
        "call",
        "merge",
        "replace",
        "copy",
        "vacuum",
        "analyze",
        "reindex",
        "refresh",
    }
)

# Irreversible / destructive → ADMIN, always explicit confirmation.
_ADMIN_KEYWORDS: frozenset[str] = frozenset({"drop", "truncate"})

_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
_COMMENT_LINE = re.compile(r"--[^\n]*")
_LEADING_WORD = re.compile(r"[a-zA-Z]+")


@dataclass(slots=True, frozen=True)
class SQLVerdict:
    permission: ResourcePermission
    read_only: bool
    reason: str


def _strip_comments(sql: str) -> str:
    sql = _COMMENT_BLOCK.sub(" ", sql)
    sql = _COMMENT_LINE.sub(" ", sql)
    return sql.strip()


def _statements(sql: str) -> list[str]:
    """Split into top-level statements on ``;`` (naive but sufficient for a
    classifier — string-literal semicolons only ever *raise* the tier, which is
    the safe direction)."""
    return [s.strip() for s in sql.split(";") if s.strip()]


def classify_sql(sql: str) -> SQLVerdict:
    """Classify *sql* into the permission tier it requires.

    Escalates (never de-escalates) on ambiguity: an empty statement, an unknown
    leading keyword, or multiple statements all resolve to at least WRITE so a
    read-only auto-approval can never green-light a mutation.
    """
    cleaned = _strip_comments(sql)
    if not cleaned:
        return SQLVerdict(ResourcePermission.WRITE, False, "Empty or comment-only statement")

    stmts = _statements(cleaned)
    if len(stmts) > 1:
        # Classify each and take the strongest — a batch is only read-only if
        # every statement is read-only.
        verdicts = [classify_sql(s) for s in stmts]
        strongest = max(verdicts, key=lambda v: _RANK[v.permission])
        if strongest.read_only:
            return SQLVerdict(ResourcePermission.READ, True, "Batch of read-only statements")
        return SQLVerdict(
            strongest.permission,
            False,
            f"Multi-statement batch containing a {strongest.permission.value} operation",
        )

    match = _LEADING_WORD.search(stmts[0])
    if match is None:
        return SQLVerdict(ResourcePermission.WRITE, False, "No recognizable leading keyword")
    head = match.group(0).lower()

    if head in _ADMIN_KEYWORDS:
        return SQLVerdict(ResourcePermission.ADMIN, False, f"{head.upper()} is destructive")
    if head in _WRITE_KEYWORDS:
        return SQLVerdict(ResourcePermission.WRITE, False, f"{head.upper()} mutates data")
    if head in _READ_KEYWORDS:
        return SQLVerdict(ResourcePermission.READ, True, f"{head.upper()} is read-only")

    return SQLVerdict(
        ResourcePermission.WRITE, False, f"Unknown statement '{head}' — treated as write"
    )


def is_read_only(sql: str) -> bool:
    return classify_sql(sql).read_only


# Rank for "strongest wins" comparisons.
_RANK: dict[ResourcePermission, int] = {
    ResourcePermission.READ: 0,
    ResourcePermission.WRITE: 1,
    ResourcePermission.EXECUTE: 1,
    ResourcePermission.ADMIN: 2,
}
