"""The deterministic assertion engine (VAIR A3): parsing, evaluation, no code execution."""

from __future__ import annotations

import json

import pytest

from velune.resources.http.assertions import (
    MAX_ASSERTIONS,
    AssertionSyntaxError,
    ResponseView,
    evaluate,
    is_missing,
    json_lookup,
    parse_assertion,
    parse_assertions,
)

DOC = {
    "data": {
        "id": 7,
        "status": "active",
        "score": 4.5,
        "flag": True,
        "tags": ["a", "b"],
        "n": None,
        "items": [{"id": 1}, {"id": 2}],
        "name": "Ada Lovelace",
    },
}


def view(status=200, headers=None, body=None, duration=100.0, text=None) -> ResponseView:
    text = text if text is not None else json.dumps(body if body is not None else DOC)
    return ResponseView(
        status,
        {
            k.lower(): v
            for k, v in (headers or {"content-type": "application/json; charset=utf-8"}).items()
        },
        text,
        duration,
    )


def check(line, **kw) -> dict:
    return evaluate([parse_assertion(line)], view(**kw))["results"][0]


# ── Status ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("line", "status", "passed"),
    [
        ("status == 200", 200, True),
        ("status == 200", 201, False),
        ("status != 500", 200, True),
        ("status != 200", 200, False),
        ("status in 200-299", 204, True),
        ("status in 200-299", 301, False),
        ("status in 200,201,202", 201, True),
        ("status in 200,201,202", 404, False),
    ],
)
def test_status_assertions(line, status, passed):
    assert check(line, status=status)["passed"] is passed


@pytest.mark.parametrize(
    "line", ["status == abc", "status", "status > 3", "status in 300-200", "status in "]
)
def test_malformed_status_assertions_are_rejected(line):
    with pytest.raises(AssertionSyntaxError):
        parse_assertion(line)


# ── Headers ─────────────────────────────────────────────────────────────────


def test_header_assertions_are_case_insensitive_on_the_name():
    assert check("header Content-Type contains application/json")["passed"]
    assert check("header content-type exists")["passed"]
    assert check("header x-missing absent")["passed"]
    assert not check("header x-missing exists")["passed"]
    assert check("header x-env == staging", headers={"X-Env": "staging"})["passed"]
    assert not check("header x-env == prod", headers={"X-Env": "staging"})["passed"]
    assert not check("header x-env == prod")["passed"]  # a missing header never equals anything


# ── JSON ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("line", "passed"),
    [
        ("json data.id exists", True),
        ("json data.nope exists", False),
        ("json data.nope absent", True),
        ("json data.id absent", False),
        ("json data.id == 7", True),
        ("json data.id == 8", False),
        ('json data.id == "7"', False),  # a string is not the number
        ("json data.status == active", True),
        ("json data.status != inactive", True),
        ("json data.flag == true", True),
        ("json data.n == null", True),
        ("json data.id type integer", True),
        ("json data.id type number", True),
        ("json data.score type integer", False),
        ("json data.score type number", True),
        ("json data.tags type array", True),
        ("json data type object", True),
        ("json data.flag type boolean", True),
        ("json data.flag type number", False),  # a boolean is not a number
        ("json data.n type null", True),
        ("json data.name contains Lovelace", True),
        ("json data.name contains Babbage", False),
        ("json data.tags contains a", True),
        ("json data.tags contains z", False),
        ("json data.score > 4", True),
        ("json data.score >= 4.5", True),
        ("json data.score < 4", False),
        ("json data.id <= 7", True),
        ("json data.items[1].id == 2", True),
        ("json data.items[5].id exists", False),
        ("json $.data.id == 7", True),
        ("json data.status == activ", False),
    ],
)
def test_json_assertions(line, passed):
    assert check(line)["passed"] is passed


def test_json_assertions_on_a_non_json_body_fail_cleanly():
    result = check("json data.id exists", text="<html>nope</html>")
    assert result["passed"] is False and "not valid JSON" in str(result["actual"])


def test_a_missing_actual_value_is_reported_as_missing():
    assert check("json data.ghost == 1")["actual"] == "<missing>"


@pytest.mark.parametrize(
    "line",
    [
        "json",
        "json data.id",
        "json data.id ??? 3",
        "json data.id type blob",
        "json data.id > abc",
        "json data..id exists",
        "json data.[x] exists",
        "nonsense here",
        "",
        "   ",
    ],
)
def test_malformed_json_assertions_are_rejected(line):
    with pytest.raises(AssertionSyntaxError):
        parse_assertion(line)


def test_json_lookup_is_bounded_and_missing_aware():
    assert json_lookup(DOC, "data.items[0].id") == 1
    assert is_missing(json_lookup(DOC, "data.items[9]"))
    with pytest.raises(AssertionSyntaxError):
        json_lookup(DOC, ".".join(["a"] * 40))


# ── Body and duration ───────────────────────────────────────────────────────


def test_body_and_duration_assertions():
    assert check("body contains active")["passed"]
    assert not check("body contains nothing-like-this")["passed"]
    assert check("body not_contains nothing-like-this")["passed"]
    assert check("duration < 500", duration=120)["passed"]
    assert not check("duration < 100", duration=100)["passed"]  # strictly below
    assert check("duration <= 100", duration=100)["passed"]
    assert not check("duration < 50", duration=120)["passed"]


@pytest.mark.parametrize(
    "line", ["duration", "duration fast", "duration > 5", "body", "body contains", "body equals x"]
)
def test_malformed_body_and_duration_assertions_are_rejected(line):
    with pytest.raises(AssertionSyntaxError):
        parse_assertion(line)


# ── Structured form ─────────────────────────────────────────────────────────


def test_structured_and_compact_forms_are_equivalent():
    structured = parse_assertion({"type": "json", "path": "data.status", "equals": "active"})
    compact = parse_assertion("json data.status == active")
    assert structured.to_dict() == compact.to_dict()
    assert evaluate([structured], view())["passed"]


@pytest.mark.parametrize(
    "item",
    [
        {"type": "status"},
        {"type": "status", "equals": "x"},
        {"type": "status", "range": [200]},
        {"type": "json", "path": "a"},
        {"type": "json", "path": "a", "exists": True, "bogus": 1},
        {"type": "header", "exists": True},
        {"type": "nope", "equals": 1},
        {"type": "duration", "max_ms": "slow"},
        42,
        None,
    ],
)
def test_malformed_structured_assertions_are_rejected(item):
    with pytest.raises(AssertionSyntaxError):
        parse_assertion(item)


def test_collections_of_assertions_are_bounded():
    assert parse_assertions(None) == []
    with pytest.raises(AssertionSyntaxError):
        parse_assertions(["status == 200"] * (MAX_ASSERTIONS + 1))
    with pytest.raises(AssertionSyntaxError):
        parse_assertions("status == 200")


# ── The summary is machine-readable and complete ────────────────────────────


def test_the_summary_lists_every_result_with_expected_and_actual():
    summary = evaluate(
        [
            parse_assertion(a)
            for a in ("status == 200", "json data.id == 9", "header x-none exists")
        ],
        view(),
    )
    assert summary["passed"] is False
    assert (summary["total"], summary["failed_count"]) == (3, 2)
    failed = [r for r in summary["results"] if not r["passed"]]
    assert {r["assertion"] for r in failed} == {"json data.id == 9", "header x-none exists"}
    assert failed[0]["expected"] == 9 and failed[0]["actual"] == 7
    json.dumps(summary)  # JSON-serializable


def test_an_empty_assertion_list_passes_vacuously():
    assert evaluate([], view()) == {"passed": True, "total": 0, "failed_count": 0, "results": []}


def test_long_actual_values_are_truncated_in_results():
    result = check("json data.name == x", body={"data": {"name": "N" * 5000}})
    assert len(result["actual"]) <= 200


# ── No code execution, no caller-supplied regexes ───────────────────────────


@pytest.mark.parametrize(
    "line",
    ["json __import__('os').system('x') exists", "status == 1 + 1", "status == __import__('os')"],
)
def test_expressions_in_structure_positions_are_refused_not_evaluated(line):
    with pytest.raises(AssertionSyntaxError):
        parse_assertion(line)


@pytest.mark.parametrize(
    ("line", "literal"),
    [
        ("json data.id == __import__('os').getcwd()", "__import__('os').getcwd()"),
        ("body contains ${7*7}", "${7*7}"),
        ("json data.name == {{secret}}", "{{secret}}"),
    ],
)
def test_expressions_in_value_positions_are_compared_as_literal_text(line, literal):
    parsed = parse_assertion(line)
    result = evaluate([parsed], view())["results"][0]
    assert result["passed"] is False
    assert literal in str(result["expected"])  # the text itself, never its evaluation


def test_a_pathological_text_cannot_cause_catastrophic_matching():
    import time

    text = "a" * 200_000
    started = time.perf_counter()
    result = evaluate([parse_assertion("body contains " + "a" * 50 + "b")], view(text=text))
    assert time.perf_counter() - started < 1.0
    assert result["passed"] is False
