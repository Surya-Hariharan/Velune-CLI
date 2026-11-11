"""Query results are bounded, JSON-safe and redacted before they reach a model (VAIR A5)."""

from __future__ import annotations

import datetime as dt
import decimal
import json
import uuid

from velune.resources.sql_results import MASK, ResultLimits, column_is_sensitive, shape_rows


def shape(columns, rows, **kw):
    limits = ResultLimits(**kw)
    return shape_rows(columns, lambda n: rows[:n], limits)


def test_rows_are_capped_and_the_cut_is_reported():
    out = shape(["n"], [[i] for i in range(50)], max_rows=10)
    assert len(out.rows) == 10 and out.truncated
    assert "row limit (10)" in out.truncated_reason
    assert out.to_data()["rowcount"] == 10 and out.to_data()["truncated"] is True


def test_a_result_within_the_limit_is_not_truncated():
    out = shape(["n"], [[1], [2]], max_rows=10)
    assert not out.truncated and out.to_data()["truncated"] is False


def test_the_fetch_is_asked_for_one_row_more_than_the_limit_never_everything():
    asked = []

    def fetch(n):
        asked.append(n)
        return [[i] for i in range(n)]

    shape_rows(["n"], fetch, ResultLimits(max_rows=7))
    assert asked == [8]


def test_total_output_is_bounded():
    big = [["x" * 400] for _ in range(100)]
    out = shape(["blob"], big, max_rows=100, max_chars=2000, max_cell_chars=500)
    assert out.truncated and "output limit" in out.truncated_reason
    assert len(json.dumps(out.to_data())) < 4000


def test_long_cells_are_shortened_and_counted():
    out = shape(["t"], [["y" * 5000]], max_cell_chars=100)
    assert len(out.rows[0][0]) < 160 and "more chars" in out.rows[0][0]
    assert out.shortened_cells == 1


def test_driver_types_become_plain_json():
    row = [
        decimal.Decimal("1.50"),
        dt.datetime(2026, 1, 2, 3, 4, 5),
        dt.date(2026, 1, 2),
        uuid.UUID(int=7),
        b"\x00\x01\x02",
        memoryview(b"abcd"),
        {"nested": [1, decimal.Decimal("2")]},
        float("nan"),
        None,
        True,
        dt.timedelta(seconds=90),
        {1, 2},
    ]
    out = shape(["c"] * len(row), [row])
    cells = out.rows[0]
    assert cells[0] == "1.50"
    assert cells[1] == "2026-01-02T03:04:05"
    assert cells[2] == "2026-01-02"
    assert cells[3] == str(uuid.UUID(int=7))
    assert cells[4] == "<binary 3 bytes>" and cells[5] == "<binary 4 bytes>"
    assert cells[6] == {"nested": [1, "2"]}
    assert cells[7] == "nan" and cells[8] is None and cells[9] is True
    json.dumps(out.to_data())  # the whole result serializes


def test_values_in_sensitive_columns_are_masked_but_nulls_stay_null():
    out = shape(
        ["id", "email", "password_hash", "API_KEY", "refresh_token"],
        [[1, "a@b.c", "$2b$12$abcdef", "sk-live-123", None]],
    )
    assert out.rows[0] == [1, "a@b.c", MASK, MASK, None]
    assert out.redacted_columns == ["password_hash", "API_KEY", "refresh_token"]


def test_configured_patterns_extend_the_defaults():
    out = shape(["ssn", "card_number", "name"], [["1", "2", "n"]], redact_columns=("ssn", "card_*"))
    assert out.rows[0] == [MASK, MASK, "n"]
    assert column_is_sensitive("CARD_NUMBER", ResultLimits(redact_columns=("card_*",)))


def test_secret_shaped_text_in_an_ordinary_column_is_scrubbed():
    out = shape(["note"], [["token is sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD"]])
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in out.rows[0][0]
