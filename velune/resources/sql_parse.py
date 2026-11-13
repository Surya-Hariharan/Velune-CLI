"""Structural SQL analysis for the database integrations (VAIR A5).

Model-written SQL is untrusted input. Keyword matching on the raw text is not enough
to decide whether a statement is read-only: a keyword can hide in a string, a comment
or a quoted identifier, and one request can carry several statements. This module
therefore **tokenizes** the text first (so strings, comments, quoted identifiers and
dollar-quoted bodies are never mistaken for code), splits it into statements, and
classifies each one from its structure — head keyword, data-modifying CTEs,
``SELECT ... INTO``, row-locking clauses, ``EXPLAIN ANALYZE`` (which *runs* its
statement), and calls to functions with side effects.

It is deliberately conservative and never the only safeguard: the connectors also run
read statements in an engine-enforced read-only transaction. The rules it follows:

* anything it cannot confirm is **not** read-only (an unknown head keyword, an
  unterminated quote, unbalanced parentheses);
* where a dialect's quoting rules are ambiguous (backslash escapes, ``--`` and ``#``
  comments, ``/*! ... */``) it analyses **every** reading and keeps the strongest result,
  so text one engine treats as a comment is never allowed to hide a statement from the
  analysis;
* administrative statements (privileges, roles, server settings, session state,
  server-side file access, dynamic SQL, transaction control) are *blocked*, not merely
  gated — they are never offered to the model.

Pure: no I/O, no driver, no clock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from velune._compat import StrEnum


class Dialect(StrEnum):
    POSTGRES = "postgres"
    MYSQL = "mysql"
    GENERIC = "generic"


class Category(StrEnum):
    """What a statement does, least to most dangerous."""

    READ = "read"
    WRITE = "write"  # changes rows (and anything not recognised)
    SCHEMA = "schema"  # changes structure: CREATE / ALTER / maintenance
    DESTRUCTIVE = "destructive"  # DROP, TRUNCATE, dropping columns, disabling RLS
    BLOCKED = "blocked"  # administrative: never executed through the integration


_RANK = {
    Category.READ: 0,
    Category.WRITE: 1,
    Category.SCHEMA: 2,
    Category.DESTRUCTIVE: 3,
    Category.BLOCKED: 4,
}


def rank(category: Category) -> int:
    return _RANK[category]


@dataclass(frozen=True, slots=True)
class StatementInfo:
    category: Category
    verb: str
    reason: str
    #: UPDATE / DELETE with no top-level WHERE: it touches every row.
    unbounded: bool = False
    #: False when the head keyword was not recognised (treated as a write).
    known: bool = True


@dataclass(frozen=True, slots=True)
class SQLAnalysis:
    statements: tuple[StatementInfo, ...]
    category: Category
    reason: str
    count: int
    unbounded: bool = False
    #: False when the text could not be tokenized cleanly (unterminated quote, ...).
    well_formed: bool = True

    @property
    def read_only(self) -> bool:
        return self.count > 0 and self.category is Category.READ and self.well_formed

    @property
    def verb(self) -> str:
        return self.statements[0].verb if self.statements else ""


# ── Tokenizer ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Tok:
    kind: str  # word | quoted | string | number | punct | param
    text: str
    depth: int = 0


class _Unterminated(Exception):
    pass


_WORD_START = re.compile(r"[A-Za-z_\u0080-￿]")
_WORD_CHAR = re.compile(r"[A-Za-z0-9_$\u0080-￿]")
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_\u0080-￿][A-Za-z0-9_\u0080-￿]*)?\$")
_PERCENT_PARAM = re.compile(r"%(?:\([A-Za-z_][A-Za-z0-9_]*\))?[sdbl%]")


def _scan_quoted(sql: str, i: int, quote: str, backslash: bool) -> int:
    """Index just past the quoted run starting at *i* (``sql[i] == quote``)."""
    n = len(sql)
    j = i + 1
    while j < n:
        ch = sql[j]
        if backslash and ch == "\\":
            j += 2
            continue
        if ch == quote:
            if j + 1 < n and sql[j + 1] == quote:  # doubled quote = literal quote
                j += 2
                continue
            return j + 1
        j += 1
    raise _Unterminated


def _tokenize(sql: str, dialect: Dialect, backslash: bool) -> list[_Tok]:
    toks: list[_Tok] = []
    n = len(sql)
    i = 0
    depth = 0
    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
            continue
        nxt = sql[i + 1] if i + 1 < n else ""
        # -- comments ------------------------------------------------------
        if ch == "-" and nxt == "-":
            after = sql[i + 2] if i + 2 < n else " "
            # PostgreSQL: always a comment. MySQL needs whitespace after ``--``.
            # Unknown dialect: only the reading both engines share.
            if dialect is Dialect.POSTGRES or after.isspace() or ord(after) < 32:
                end = sql.find("\n", i)
                i = n if end < 0 else end + 1
                continue
        if ch == "#" and dialect is Dialect.MYSQL:
            end = sql.find("\n", i)
            i = n if end < 0 else end + 1
            continue
        if ch == "/" and nxt == "*":
            if dialect is not Dialect.POSTGRES and sql.startswith("/*!", i):
                # MySQL executes the body of a conditional comment: it is code.
                j = i + 3
                while j < n and sql[j].isdigit():
                    j += 1
                end = sql.find("*/", j)
                if end < 0:
                    raise _Unterminated
                toks.extend(_tokenize(sql[j:end], dialect, backslash))
                i = end + 2
                continue
            if dialect is Dialect.POSTGRES:  # block comments nest
                level, j = 1, i + 2
                while j < n and level:
                    if sql.startswith("/*", j):
                        level += 1
                        j += 2
                    elif sql.startswith("*/", j):
                        level -= 1
                        j += 2
                    else:
                        j += 1
                if level:
                    raise _Unterminated
                i = j
            else:  # ends at the first */ — never hides more than the engine does
                end = sql.find("*/", i + 2)
                if end < 0:
                    raise _Unterminated
                i = end + 2
            continue
        # -- quoted runs ---------------------------------------------------
        if ch == "'":
            esc = backslash or (
                dialect is Dialect.POSTGRES
                and toks
                and toks[-1].kind == "word"
                and toks[-1].text.lower() == "e"
                and sql[i - 1] in "eE"
            )
            j = _scan_quoted(sql, i, "'", bool(esc))
            toks.append(_Tok("string", sql[i:j], depth))
            i = j
            continue
        if ch in '"`':
            j = _scan_quoted(sql, i, ch, backslash and ch == '"' and dialect is Dialect.MYSQL)
            toks.append(_Tok("quoted", sql[i:j], depth))
            i = j
            continue
        if ch == "$":
            prev_word = i > 0 and _WORD_CHAR.match(sql[i - 1]) is not None
            if dialect is Dialect.POSTGRES and not prev_word:
                m = _DOLLAR_TAG.match(sql, i)
                if m:
                    tag = m.group(0)
                    end = sql.find(tag, m.end())
                    if end < 0:
                        raise _Unterminated
                    toks.append(_Tok("string", sql[i : end + len(tag)], depth))
                    i = end + len(tag)
                    continue
            if i + 1 < n and sql[i + 1].isdigit():
                j = i + 1
                while j < n and sql[j].isdigit():
                    j += 1
                toks.append(_Tok("param", sql[i:j], depth))
                i = j
                continue
        if ch == "%":
            m = _PERCENT_PARAM.match(sql, i)
            if m:
                toks.append(_Tok("param", m.group(0), depth))
                i = m.end()
                continue
        if ch == "?":
            toks.append(_Tok("param", "?", depth))
            i += 1
            continue
        # -- words and numbers --------------------------------------------
        if ch == "@":
            j = i + 1
            while j < n and (sql[j] == "@" or _WORD_CHAR.match(sql[j]) or sql[j] == "."):
                j += 1
            toks.append(_Tok("word", sql[i:j], depth))
            i = j
            continue
        if _WORD_START.match(ch):
            j = i + 1
            while j < n and _WORD_CHAR.match(sql[j]):
                j += 1
            toks.append(_Tok("word", sql[i:j], depth))
            i = j
            continue
        if ch.isdigit():
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] in "._"):
                j += 1
            toks.append(_Tok("number", sql[i:j], depth))
            i = j
            continue
        # -- punctuation ---------------------------------------------------
        if ch == "(":
            toks.append(_Tok("punct", "(", depth))
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise _Unterminated
            toks.append(_Tok("punct", ")", depth))
        elif ch == ":" and nxt == ":":
            toks.append(_Tok("punct", "::", depth))
            i += 2
            continue
        else:
            toks.append(_Tok("punct", ch, depth))
        i += 1
    if depth != 0:
        raise _Unterminated
    return toks


def _split(toks: list[_Tok]) -> list[list[_Tok]]:
    statements: list[list[_Tok]] = []
    current: list[_Tok] = []
    for tok in toks:
        if tok.kind == "punct" and tok.text == ";":
            if current:
                statements.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        statements.append(current)
    return statements


# ── Statement classification ─────────────────────────────────────────────────

_READ_HEADS = frozenset({"select", "values", "table", "show", "describe", "desc"})
_DML_HEADS = frozenset({"insert", "update", "delete", "replace", "merge", "upsert"})
_SCHEMA_HEADS = frozenset(
    {
        "create",
        "alter",
        "comment",
        "rename",
        "reindex",
        "cluster",
        "refresh",
        "vacuum",
        "analyze",
        "analyse",
        "optimize",
        "repair",
    }
)
_DESTRUCTIVE_HEADS = frozenset({"drop", "truncate"})
#: Never run through the integration: privileges, session state, transaction control
#: (the transaction operations own that), dynamic SQL, server files and processes.
_BLOCKED_HEADS = {
    "grant": "changes privileges",
    "revoke": "changes privileges",
    "set": "changes session state (use the connection's settings instead)",
    "reset": "changes session state",
    "use": "switches database (pick another connection instead)",
    "begin": "controls transactions (use the transaction operations)",
    "start": "controls transactions (use the transaction operations)",
    "commit": "controls transactions (use the transaction operations)",
    "rollback": "controls transactions (use the transaction operations)",
    "end": "controls transactions (use the transaction operations)",
    "abort": "controls transactions (use the transaction operations)",
    "savepoint": "controls transactions (use the transaction operations)",
    "release": "controls transactions (use the transaction operations)",
    "prepare": "dynamic SQL cannot be inspected",
    "execute": "dynamic SQL cannot be inspected",
    "deallocate": "dynamic SQL cannot be inspected",
    "do": "runs an anonymous code block that cannot be inspected",
    "copy": "reads or writes server-side files or programs",
    "load": "reads files",
    "kill": "administers server processes",
    "shutdown": "administers the server",
    "install": "administers the server",
    "uninstall": "administers the server",
    "flush": "administers the server",
    "purge": "administers the server",
    "change": "administers replication",
    "stop": "administers the server",
    "listen": "holds session state",
    "unlisten": "holds session state",
    "discard": "changes session state",
    "import": "reads files",
    "attach": "attaches another database",
    "detach": "detaches a database",
    "checkpoint": "administers the server",
    "lock": "takes table locks (use a transaction)",
    "handler": "uses low-level table handlers",
}
_BLOCKED_OBJECTS = frozenset(
    {"role", "user", "group", "extension", "server", "foreign", "language", "tablespace"}
)
_ALTER_BLOCKED = frozenset(
    {"role", "user", "group", "system", "extension", "server", "tablespace", "default", "database"}
)
_BLOCKED_FUNCTIONS = frozenset(
    {
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_ls_logdir",
        "pg_ls_waldir",
        "pg_stat_file",
        "pg_file_write",
        "lo_import",
        "lo_export",
        "dblink",
        "dblink_exec",
        "dblink_connect",
        "set_config",
        "pg_switch_wal",
        "pg_create_restore_point",
        "pg_promote",
        "load_file",
        "sys_exec",
        "sys_eval",
    }
)
_WRITE_FUNCTIONS = frozenset(
    {
        "nextval",
        "setval",
        "lo_create",
        "lo_unlink",
        "lo_put",
        "pg_advisory_lock",
        "pg_advisory_xact_lock",
        "pg_try_advisory_lock",
        "pg_try_advisory_xact_lock",
        "get_lock",
        "release_lock",
    }
)


def _words(toks: list[_Tok]) -> list[str]:
    return [t.text.lower() for t in toks if t.kind == "word"]


def _function_names(toks: list[_Tok]) -> list[str]:
    """Names immediately followed by ``(`` (the last part of a qualified name)."""
    out: list[str] = []
    for a, b in zip(toks, toks[1:], strict=False):
        if b.kind == "punct" and b.text == "(" and a.kind in ("word", "quoted"):
            out.append(a.text.strip('"`').lower())
    return out


def _unbounded(toks: list[_Tok]) -> bool:
    return not any(t.kind == "word" and t.text.lower() == "where" and t.depth == 0 for t in toks)


def _info(category: Category, verb: str, reason: str, **kw: bool) -> StatementInfo:
    return StatementInfo(category, verb, reason, **kw)


def _escalate_for_functions(info: StatementInfo, toks: list[_Tok]) -> StatementInfo:
    names = set(_function_names(toks))
    blocked = sorted(names & _BLOCKED_FUNCTIONS)
    if blocked:
        return StatementInfo(
            Category.BLOCKED,
            info.verb,
            f"calls {blocked[0]}(), an administrative function",
            info.unbounded,
        )
    side = sorted(names & _WRITE_FUNCTIONS)
    if side and rank(info.category) < rank(Category.WRITE):
        return StatementInfo(
            Category.WRITE, info.verb, f"calls {side[0]}(), which has side effects", info.unbounded
        )
    return info


def _classify_read_body(toks: list[_Tok], verb: str) -> StatementInfo:
    """A SELECT-shaped statement: read unless INTO / a row lock says otherwise."""
    words = _words(toks)
    for idx, tok in enumerate(toks):
        if tok.kind != "word":
            continue
        low = tok.text.lower()
        if low == "into" and tok.depth == 0:
            follow = toks[idx + 1].text.lower() if idx + 1 < len(toks) else ""
            if follow in ("outfile", "dumpfile"):
                return _info(Category.BLOCKED, verb, "writes a file on the server")
            return _info(Category.WRITE, verb, "SELECT ... INTO creates a table or sets variables")
        if low == "for" and idx + 1 < len(toks):
            if toks[idx + 1].text.lower() in ("update", "share", "no", "key"):
                return _info(Category.WRITE, verb, "locks the rows it reads")
    if "lock" in words and "share" in words and "mode" in words:
        return _info(Category.WRITE, verb, "locks the rows it reads")
    return _info(Category.READ, verb, f"{verb.upper()} is read-only")


def _classify_cte(toks: list[_Tok]) -> StatementInfo:
    """``WITH ...``: read unless a CTE or the main statement modifies data."""
    for idx, tok in enumerate(toks):
        if tok.kind == "punct" and tok.text == "(" and idx + 1 < len(toks):
            inner = toks[idx + 1]
            if inner.kind == "word" and inner.text.lower() in _DML_HEADS:
                return _info(
                    Category.WRITE,
                    "with",
                    f"a CTE runs {inner.text.upper()}",
                    unbounded=inner.text.lower() in ("update", "delete") and _unbounded(toks),
                )
    for tok in toks:
        if tok.kind == "word" and tok.depth == 0:
            low = tok.text.lower()
            if low in _DML_HEADS:
                return _info(
                    Category.WRITE,
                    "with",
                    f"WITH ... {low.upper()} changes rows",
                    unbounded=low in ("update", "delete") and _unbounded(toks),
                )
            if low in ("select", "values", "table"):
                break
    return _classify_read_body(toks, "with")


_STATEMENT_START = frozenset(
    {"select", "with", "insert", "update", "delete", "values", "table", "replace", "merge"}
)


def _classify_explain(toks: list[_Tok]) -> StatementInfo:
    """``EXPLAIN`` plans a statement; with ANALYZE it *runs* it."""
    analyze = False
    inner_at = len(toks)
    for idx in range(1, len(toks)):
        tok = toks[idx]
        if tok.kind != "word":
            continue
        low = tok.text.lower()
        if low in ("analyze", "analyse"):
            analyze = True
        if tok.depth == 0 and low in _STATEMENT_START:
            inner_at = idx
            break
    inner = toks[inner_at:]
    if not analyze or not inner:
        return _info(Category.READ, "explain", "EXPLAIN plans without running")
    body = _classify(inner)
    if body.category is Category.READ:
        return _info(Category.READ, "explain", "EXPLAIN ANALYZE runs a read-only statement")
    return _info(
        body.category,
        "explain",
        f"EXPLAIN ANALYZE would run: {body.reason}",
        unbounded=body.unbounded,
        known=body.known,
    )


def _classify(toks: list[_Tok]) -> StatementInfo:
    start = 0
    while start < len(toks) and toks[start].text == "(":
        start += 1
    toks = toks[start:]
    if not toks or toks[0].kind != "word":
        return _info(Category.WRITE, "", "no recognizable leading keyword", known=False)
    head = toks[0].text.lower()
    second = toks[1].text.lower() if len(toks) > 1 and toks[1].kind == "word" else ""

    if head == "explain":
        info = _classify_explain(toks)
    elif head == "with":
        info = _classify_cte(toks)
    elif head in _READ_HEADS:
        info = _classify_read_body(toks, head)
    elif head in _DML_HEADS:
        info = _info(
            Category.WRITE,
            head,
            f"{head.upper()} changes rows",
            unbounded=head in ("update", "delete") and _unbounded(toks),
        )
    elif head in _DESTRUCTIVE_HEADS:
        info = _info(Category.DESTRUCTIVE, head, f"{head.upper()} is destructive")
    elif head in _BLOCKED_HEADS:
        info = _info(Category.BLOCKED, head, f"{head.upper()} {_BLOCKED_HEADS[head]}")
    elif head in _SCHEMA_HEADS:
        info = _classify_schema(head, second, toks)
    elif head == "call":
        info = _info(Category.WRITE, head, "CALL runs a procedure that can change anything")
    else:
        info = _info(Category.WRITE, head, f"unrecognized statement '{head}'", known=False)
    if info.category is not Category.BLOCKED:
        info = _escalate_for_functions(info, toks)
    return info


def _classify_schema(head: str, second: str, toks: list[_Tok]) -> StatementInfo:
    words = _words(toks)
    if head == "create" and second in _BLOCKED_OBJECTS:
        return _info(Category.BLOCKED, head, f"CREATE {second.upper()} administers the server")
    if head == "create" and second == "or" and "role" in words[:4]:
        return _info(Category.BLOCKED, head, "creates a role")
    if head == "alter" and second in _ALTER_BLOCKED:
        return _info(Category.BLOCKED, head, f"ALTER {second.upper()} administers the server")
    if head == "alter" and "drop" in words:
        return _info(Category.DESTRUCTIVE, head, "ALTER ... DROP removes structure")
    if head == "alter" and "row" in words and "security" in words:
        if "disable" in words or "no" in words:
            return _info(Category.DESTRUCTIVE, head, "turns off row-level security on a table")
    if head in ("vacuum", "analyze", "analyse", "optimize", "repair", "cluster", "reindex"):
        return _info(Category.SCHEMA, head, f"{head.upper()} is a maintenance command")
    return _info(Category.SCHEMA, head, f"{head.upper()} changes structure")


# ── Public API ───────────────────────────────────────────────────────────────


def _variants(dialect: Dialect) -> tuple[bool, ...]:
    # Backslash handling differs by engine *and* by server settings, so both readings
    # are analysed; the strongest result wins.
    return (True, False) if dialect is Dialect.MYSQL else (False, True)


def analyze_sql(sql: str, dialect: Dialect = Dialect.GENERIC) -> SQLAnalysis:
    """Classify *sql* (one or more statements). Never raises."""
    results: list[SQLAnalysis] = []
    for backslash in _variants(dialect):
        try:
            toks = _tokenize(str(sql or ""), dialect, backslash)
        except (_Unterminated, RecursionError):
            results.append(
                SQLAnalysis(
                    (),
                    Category.WRITE,
                    "could not be parsed (unterminated quote, comment or parenthesis)",
                    count=1,
                    well_formed=False,
                )
            )
            continue
        statements = _split(toks)
        if not statements:
            results.append(
                SQLAnalysis((), Category.WRITE, "Empty or comment-only statement", count=0)
            )
            continue
        infos = tuple(_classify(s) for s in statements)
        strongest = max(infos, key=lambda i: rank(i.category))
        reason = strongest.reason
        if len(infos) > 1:
            reason = f"{len(infos)} statements; strongest: {strongest.reason}"
        results.append(
            SQLAnalysis(
                infos,
                strongest.category,
                reason,
                count=len(infos),
                unbounded=any(i.unbounded for i in infos),
                well_formed=True,
            )
        )
    worst = max(results, key=lambda r: (not r.well_formed, rank(r.category), r.count))
    return SQLAnalysis(
        worst.statements,
        worst.category,
        worst.reason,
        count=max(r.count for r in results),
        unbounded=any(r.unbounded for r in results),
        well_formed=all(r.well_formed for r in results),
    )


class StatementError(ValueError):
    """The request is not exactly one acceptable statement."""


def require_single_statement(analysis: SQLAnalysis) -> None:
    if not analysis.well_formed:
        raise StatementError("The SQL could not be parsed; check quotes and parentheses.")
    if analysis.count == 0:
        raise StatementError("The SQL is empty.")
    if analysis.count > 1:
        raise StatementError(
            f"Send exactly one statement per call (found {analysis.count}); "
            "use a transaction to group several."
        )


__all__ = [
    "Category",
    "Dialect",
    "SQLAnalysis",
    "StatementError",
    "StatementInfo",
    "analyze_sql",
    "rank",
    "require_single_statement",
]
