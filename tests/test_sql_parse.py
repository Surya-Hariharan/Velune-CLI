"""Structural SQL analysis: what is read-only, what is not, and what is never run (VAIR A5)."""

from __future__ import annotations

import pytest

from velune.resources.base import ResourcePermission
from velune.resources.sql_parse import (
    Category,
    Dialect,
    StatementError,
    analyze_sql,
    require_single_statement,
)
from velune.resources.sql_safety import classify_sql

PG, MY = Dialect.POSTGRES, Dialect.MYSQL
R, W, S, D, B = (
    Category.READ,
    Category.WRITE,
    Category.SCHEMA,
    Category.DESTRUCTIVE,
    Category.BLOCKED,
)


def cat(sql: str, dialect: Dialect = PG) -> Category:
    return analyze_sql(sql, dialect).category


# ── Plain statements ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select * from users where id = %s",
        "  SELECT a, b FROM t ORDER BY a LIMIT 10  ",
        "SELECT 1;",
        "VALUES (1), (2)",
        "TABLE users",
        "SHOW TABLES",
        "DESCRIBE users",
        "(SELECT 1) UNION (SELECT 2)",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "EXPLAIN SELECT * FROM t",
        "EXPLAIN (FORMAT JSON) SELECT * FROM t",
        "EXPLAIN DELETE FROM t",  # planning only: nothing runs
        "SELECT 'a;b' AS s",
        'SELECT "select" FROM t',
    ],
)
def test_reads_are_read_only(sql):
    a = analyze_sql(sql, PG)
    assert a.read_only and a.count == 1 and a.category is R


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("INSERT INTO t VALUES (1)", W),
        ("UPDATE t SET a = 1 WHERE id = 2", W),
        ("DELETE FROM t WHERE id = 1", W),
        ("REPLACE INTO t VALUES (1)", W),
        ("CALL do_things()", W),
        ("CREATE TABLE t (id int)", S),
        ("ALTER TABLE t ADD COLUMN c int", S),
        ("CREATE INDEX i ON t (a)", S),
        ("VACUUM t", S),
        ("DROP TABLE t", D),
        ("TRUNCATE t", D),
        ("ALTER TABLE t DROP COLUMN c", D),
        ("ALTER TABLE t DISABLE ROW LEVEL SECURITY", D),
    ],
)
def test_writes_are_classified_by_what_they_do(sql, expected):
    a = analyze_sql(sql, PG)
    assert a.category is expected and not a.read_only


# ── Statements are counted, and nothing hides in strings or comments ────────


def test_two_statements_are_two_statements():
    a = analyze_sql("SELECT 1; DROP TABLE t", PG)
    assert a.count == 2 and a.category is D
    with pytest.raises(StatementError, match="exactly one"):
        require_single_statement(a)


def test_a_batch_of_reads_is_still_a_batch():
    a = analyze_sql("SELECT 1; SELECT 2", PG)
    assert a.count == 2 and a.read_only  # all reads, but never run as one call
    with pytest.raises(StatementError):
        require_single_statement(a)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'x; DROP TABLE t'",
        "SELECT $$x; DROP TABLE t$$",
        "SELECT $tag$x; DROP TABLE t$tag$",
        "SELECT 1 /* ; DROP TABLE t */",
        "SELECT 1 /* a /* nested ; DROP TABLE t */ still comment */",
        "SELECT 1 -- ; DROP TABLE t",
        'SELECT "a;b" FROM t',
        "SELECT E'it\\'s; DROP TABLE t' ",
    ],
)
def test_postgres_quotes_and_comments_hide_nothing_and_nothing_is_hidden_by_them(sql):
    a = analyze_sql(sql, PG)
    assert a.count == 1 and a.category is R


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 --x; DROP TABLE t",  # MySQL: not a comment without a space
        "SELECT 1 /*!50000 ; DROP TABLE t */",  # MySQL executes a conditional comment
        "SELECT '\\'; DROP TABLE t; --'",  # a backslash may or may not escape: both are checked
    ],
)
def test_mysql_comment_and_escape_ambiguity_never_hides_a_statement(sql):
    a = analyze_sql(sql, MY)
    assert not a.read_only  # it is either two statements or unparseable, never a clean read


def test_a_hash_comment_cannot_hide_a_statement_in_postgres():
    # '#' is an operator in PostgreSQL, so what follows is code.
    a = analyze_sql("SELECT 1 # ; DROP TABLE t", PG)
    assert a.count == 2 and a.category is D


def test_hash_is_a_comment_in_mysql():
    a = analyze_sql("SELECT 1 # ; DROP TABLE t", MY)
    assert a.count == 1 and a.read_only


@pytest.mark.parametrize(
    "sql", ["SELECT 'unterminated", "SELECT (1", "SELECT 1 /* open", "SELECT $$ open", 'SELECT "x']
)
def test_text_that_cannot_be_parsed_is_not_a_read(sql):
    a = analyze_sql(sql, PG)
    assert not a.well_formed and not a.read_only
    with pytest.raises(StatementError, match="could not be parsed"):
        require_single_statement(a)


def test_empty_and_comment_only_text_is_not_a_read():
    for sql in ("", "   ", "-- nothing", "/* nothing */"):
        a = analyze_sql(sql, PG)
        assert a.count == 0 and not a.read_only
        with pytest.raises(StatementError, match="empty"):
            require_single_statement(a)


# ── Reads that are not really reads ─────────────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
        "WITH i AS (INSERT INTO t VALUES (1) RETURNING id) SELECT * FROM i",
        "WITH u AS (UPDATE t SET a = 1 RETURNING *) SELECT * FROM u",
        "WITH x AS (SELECT 1) DELETE FROM t",
        "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
    ],
)
def test_a_data_modifying_cte_is_a_write(sql):
    a = analyze_sql(sql, PG)
    assert a.category is W and not a.read_only


@pytest.mark.parametrize(
    "sql",
    [
        "EXPLAIN ANALYZE DELETE FROM t",
        "EXPLAIN (ANALYZE, BUFFERS) UPDATE t SET a = 1 WHERE id = 1",
        "EXPLAIN ANALYZE INSERT INTO t VALUES (1)",
    ],
)
def test_explain_analyze_runs_its_statement(sql):
    a = analyze_sql(sql, PG)
    assert a.category is W and not a.read_only


def test_explain_analyze_of_a_read_is_a_read():
    assert analyze_sql("EXPLAIN ANALYZE SELECT 1", PG).read_only


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT * INTO new_table FROM t", W),
        ("SELECT * FROM t FOR UPDATE", W),
        ("SELECT * FROM t FOR SHARE", W),
        ("SELECT * FROM t LOCK IN SHARE MODE", W),
        ("SELECT * FROM t INTO OUTFILE '/tmp/x'", B),
        ("SELECT nextval('s')", W),
        ("SELECT setval('s', 1)", W),
        ("SELECT pg_terminate_backend(1)", B),
        ("SELECT pg_catalog.set_config('transaction_read_only', 'off', false)", B),
        ("SELECT pg_read_file('/etc/passwd')", B),
        ("SELECT lo_import('/etc/passwd')", B),
        ("SELECT load_file('/etc/passwd')", B),
    ],
)
def test_selects_with_side_effects_are_not_reads(sql, expected):
    assert cat(sql, PG) is expected


def test_a_column_named_like_a_keyword_is_not_a_function_call():
    assert analyze_sql('SELECT "nextval" FROM t', PG).read_only
    assert analyze_sql("SELECT insert('abc', 1, 1, 'x')", MY).read_only  # MySQL string function


# ── Administrative statements are blocked, never merely gated ───────────────


@pytest.mark.parametrize(
    "sql",
    [
        "GRANT ALL ON t TO someone",
        "REVOKE ALL ON t FROM someone",
        "SET default_transaction_read_only = off",
        "SET ROLE admin",
        "RESET ALL",
        "BEGIN",
        "COMMIT",
        "ROLLBACK",
        "START TRANSACTION",
        "SAVEPOINT x",
        "PREPARE p AS SELECT 1",
        "EXECUTE p",
        "DO $$ BEGIN PERFORM 1; END $$",
        "COPY t TO PROGRAM 'cat'",
        "COPY t FROM '/etc/passwd'",
        "LOAD DATA LOCAL INFILE '/etc/passwd' INTO TABLE t",
        "KILL 5",
        "USE other_db",
        "CREATE ROLE evil SUPERUSER",
        "CREATE USER evil",
        "ALTER ROLE x SUPERUSER",
        "ALTER SYSTEM SET x = 1",
        "CREATE EXTENSION dblink",
        "LOCK TABLE t",
        "LISTEN chan",
    ],
)
def test_administrative_statements_are_blocked(sql):
    a = analyze_sql(sql, PG if "LOAD" not in sql and "KILL" not in sql else MY)
    assert a.category is B


# ── Unbounded changes are flagged ───────────────────────────────────────────


def test_a_delete_or_update_without_where_is_flagged_unbounded():
    assert analyze_sql("DELETE FROM t", PG).unbounded
    assert analyze_sql("UPDATE t SET a = 1", PG).unbounded
    assert not analyze_sql("DELETE FROM t WHERE id = 1", PG).unbounded
    assert not analyze_sql("UPDATE t SET a = 1 WHERE id = 1", PG).unbounded
    # a WHERE inside a subquery does not bound the outer statement
    assert analyze_sql("DELETE FROM t USING (SELECT 1 WHERE true) s", PG).unbounded


# ── Fail closed on anything unknown ─────────────────────────────────────────


def test_an_unknown_statement_is_treated_as_a_write():
    a = analyze_sql("GIBBERISH foo", PG)
    assert a.category is W and not a.read_only and not a.statements[0].known


# ── The long-standing API (permission tiers) is preserved ───────────────────


@pytest.mark.parametrize(
    ("sql", "tier", "read_only"),
    [
        ("SELECT * FROM users", ResourcePermission.READ, True),
        ("INSERT INTO t VALUES (1)", ResourcePermission.WRITE, False),
        ("CREATE TABLE t (id int)", ResourcePermission.WRITE, False),
        ("DROP TABLE t", ResourcePermission.ADMIN, False),
        ("GRANT ALL ON t TO x", ResourcePermission.ADMIN, False),
        ("SELECT 1; DROP TABLE t", ResourcePermission.ADMIN, False),
        ("", ResourcePermission.WRITE, False),
    ],
)
def test_classify_sql_maps_to_permission_tiers(sql, tier, read_only):
    verdict = classify_sql(sql)
    assert verdict.permission is tier and verdict.read_only is read_only


def test_analysis_never_raises_on_hostile_text():
    for sql in (
        "\x00",
        "(" * 5000,
        "'" * 3,
        "$" * 50,
        ";" * 100,
        "SELECT " + "(" * 2000 + "1" + ")" * 2000,
    ):
        analyze_sql(sql, PG)
        analyze_sql(sql, MY)
