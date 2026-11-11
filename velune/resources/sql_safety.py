"""SQL statement classification for the database connectors.

Maps a statement to the :class:`~velune.resources.base.ResourcePermission` it
requires so the permission pipeline can gate it:

    SELECT / EXPLAIN / SHOW / DESCRIBE   → READ   (no confirmation in any mode)
    INSERT / UPDATE / DELETE / DDL        → WRITE  (confirmation required)
    DROP / TRUNCATE / administrative      → ADMIN  (explicit confirmation / blocked)

Since VAIR A5 this is built on the structural analysis in
:mod:`velune.resources.sql_parse` (a real tokenizer, not a leading-keyword guess), so
strings, comments and quoted identifiers cannot hide a statement, a data-modifying
CTE or ``EXPLAIN ANALYZE`` of a write is not mistaken for a read, and anything that
cannot be confirmed read-only is escalated — never the other way round.
"""

from __future__ import annotations

from dataclasses import dataclass

from velune.resources.base import ResourcePermission
from velune.resources.sql_parse import Category, Dialect, SQLAnalysis, analyze_sql


@dataclass(slots=True, frozen=True)
class SQLVerdict:
    permission: ResourcePermission
    read_only: bool
    reason: str


_TIER = {
    Category.READ: ResourcePermission.READ,
    Category.WRITE: ResourcePermission.WRITE,
    Category.SCHEMA: ResourcePermission.WRITE,
    Category.DESTRUCTIVE: ResourcePermission.ADMIN,
    Category.BLOCKED: ResourcePermission.ADMIN,
}


def verdict_for(analysis: SQLAnalysis) -> SQLVerdict:
    if analysis.read_only:
        return SQLVerdict(ResourcePermission.READ, True, analysis.reason)
    return SQLVerdict(_TIER[analysis.category], False, analysis.reason)


def classify_sql(sql: str, dialect: Dialect = Dialect.GENERIC) -> SQLVerdict:
    """Classify *sql* into the permission tier it requires.

    Escalates (never de-escalates) on ambiguity: an empty statement, an unknown
    leading keyword, text that cannot be parsed, or a batch containing anything
    other than reads all resolve to at least WRITE.
    """
    return verdict_for(analyze_sql(sql, dialect))


def is_read_only(sql: str, dialect: Dialect = Dialect.GENERIC) -> bool:
    return classify_sql(sql, dialect).read_only
