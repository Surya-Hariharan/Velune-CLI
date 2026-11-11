"""Bounded, JSON-safe, redacted query results (VAIR A5).

What a database returns goes to a language model, so it is shaped before it leaves
the connector:

* **bounded** — at most ``max_rows`` rows, ``max_cell_chars`` per value and
  ``max_chars`` in total; the result says plainly when it was cut and why;
* **JSON-compatible** — dates, decimals, UUIDs, binary and driver types become plain
  JSON values, never ``repr`` noise or an exception;
* **redacted** — values in columns whose name looks sensitive (``password``,
  ``token``, ``secret``, …, plus any configured pattern) are masked, and secret-shaped
  text in other cells is scrubbed.

Pure: operates on what a DB-API cursor already produced.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import fnmatch
import math
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

MASK = "***"
#: Column names masked by default, matched as case-insensitive substrings.
DEFAULT_SENSITIVE_COLUMNS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "credential",
    "auth_key",
    "access_key",
    "refresh_token",
    "session_key",
)


@dataclass(frozen=True, slots=True)
class ResultLimits:
    max_rows: int = 200
    max_cell_chars: int = 500
    max_chars: int = 20_000
    #: Extra column-name patterns to mask (``fnmatch`` style, case-insensitive).
    redact_columns: tuple[str, ...] = ()
    #: Mask the default sensitive column names too (turn off only for tests).
    default_redaction: bool = True


@dataclass(slots=True)
class ShapedResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool = False
    truncated_reason: str = ""
    redacted_columns: list[str] = field(default_factory=list)
    #: Cells shortened because they exceeded ``max_cell_chars``.
    shortened_cells: int = 0

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "columns": self.columns,
            "rows": self.rows,
            "rowcount": len(self.rows),
            "truncated": self.truncated,
        }
        if self.truncated_reason:
            data["truncated_reason"] = self.truncated_reason
        if self.redacted_columns:
            data["redacted_columns"] = self.redacted_columns
        if self.shortened_cells:
            data["shortened_cells"] = self.shortened_cells
        return data


def column_is_sensitive(name: str, limits: ResultLimits) -> bool:
    low = str(name).lower()
    if limits.default_redaction and any(p in low for p in DEFAULT_SENSITIVE_COLUMNS):
        return True
    return any(fnmatch.fnmatchcase(low, pat.lower()) for pat in limits.redact_columns)


def json_safe(value: Any, limits: ResultLimits, counter: list[int] | None = None) -> Any:
    """Convert one driver value to a JSON value, shortening long text."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, str):
        return _shorten(value, limits, counter)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary {len(bytes(value))} bytes>"
    if isinstance(value, dict):
        return {str(k)[:100]: json_safe(v, limits, counter) for k, v in list(value.items())[:100]}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(v, limits, counter) for v in list(value)[:100]]
    return _shorten(str(value), limits, counter)


def _shorten(text: str, limits: ResultLimits, counter: list[int] | None) -> str:
    from velune.core.redaction import redact_secrets

    text = redact_secrets(text)
    if len(text) <= limits.max_cell_chars:
        return text
    if counter is not None:
        counter[0] += 1
    return text[: limits.max_cell_chars] + f"... [{len(text) - limits.max_cell_chars} more chars]"


def shape_rows(
    columns: Sequence[str],
    fetch: Callable[[int], Iterable[Sequence[Any]]],
    limits: ResultLimits,
) -> ShapedResult:
    """Shape rows fetched through *fetch(n)* (a ``fetchmany``-style callable).

    ``fetch`` is asked for at most ``max_rows + 1`` rows so truncation is detected
    without reading an unbounded result set into memory.
    """
    names = [str(c) for c in columns]
    sensitive = [i for i, c in enumerate(names) if column_is_sensitive(c, limits)]
    out = ShapedResult(columns=names, rows=[])
    out.redacted_columns = [names[i] for i in sensitive]
    counter = [0]
    budget = limits.max_chars
    raw = list(fetch(limits.max_rows + 1))
    for index, row in enumerate(raw):
        if index >= limits.max_rows:
            out.truncated = True
            out.truncated_reason = f"row limit ({limits.max_rows}) reached; narrow the query"
            break
        shaped = [
            (MASK if i in sensitive and v is not None else json_safe(v, limits, counter))
            for i, v in enumerate(row)
        ]
        cost = sum(len(str(v)) + 2 for v in shaped)
        if out.rows and cost > budget:
            out.truncated = True
            out.truncated_reason = (
                f"output limit ({limits.max_chars} characters) reached; select fewer columns"
            )
            break
        budget -= cost
        out.rows.append(shaped)
    out.shortened_cells = counter[0]
    return out
