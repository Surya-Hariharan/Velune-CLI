"""A deterministic assertion engine for HTTP responses (VAIR A3).

Assertions are *data*, evaluated locally by plain comparisons — never by asking a
model whether a response "looks right", and never by evaluating user-supplied
code or expressions. There is no ``eval``, no regular expression supplied by the
caller (so no catastrophic backtracking) and no templating here.

Two spellings produce the same normalized form:

* a compact line a model can write in a flat argument list::

      status == 200
      status in 200-299
      header content-type contains application/json
      json data.id exists
      json data.status == active
      json data.items type array
      body contains welcome
      duration < 500

* a structured dict, which is what API collections store::

      {"type": "json", "path": "data.status", "equals": "active"}

Supported checks: status (equals / not / in a list / in a range), header
(exists / absent / equals / contains), JSON path (exists / absent / equals / not
equals / type / contains / numeric comparison), body (contains / not contains)
and duration (a maximum, in milliseconds).

JSON paths are dotted keys and ``[index]`` steps (``data.items[0].id``); a
leading ``$.`` is accepted. Paths do not support wildcards or filters.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

MAX_ASSERTIONS = 50
_MAX_SHOWN = 200
_MISSING = object()
_PATH_STEP = re.compile(r"\.?([A-Za-z0-9_\-]+)|\[(\d{1,6})\]|\['([^']{1,200})'\]")
_TYPES = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}
_NUMERIC_OPS = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
_SYMBOLS = {v: k for k, v in _NUMERIC_OPS.items()}


class AssertionSyntaxError(ValueError):
    """An assertion that cannot be understood (message is safe to show the model)."""


@dataclass(frozen=True, slots=True)
class Assertion:
    """One normalized assertion: ``kind`` plus its parameters."""

    kind: str
    params: tuple[tuple[str, Any], ...]

    def get(self, key: str, default: Any = None) -> Any:
        for name, value in self.params:
            if name == key:
                return value
        return default

    def has(self, key: str) -> bool:
        """True when *key* is set — even to ``None`` (``== null`` is a real check)."""
        return any(name == key for name, _ in self.params)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.kind, **dict(self.params)}

    def describe(self) -> str:
        kind = self.kind
        if kind == "status":
            if self.get("range"):
                low, high = self.get("range")
                return f"status in {low}-{high}"
            if self.get("in") is not None:
                return "status in " + ",".join(str(s) for s in self.get("in"))
            if self.get("not") is not None:
                return f"status != {self.get('not')}"
            return f"status == {self.get('equals')}"
        if kind == "duration":
            return f"duration <= {self.get('max_ms')}ms"
        if kind == "body":
            if self.get("contains") is not None:
                return f"body contains {self.get('contains')!r}"
            return f"body does not contain {self.get('not_contains')!r}"
        label = f"header {self.get('name')}" if kind == "header" else f"json {self.get('path')}"
        for key in ("exists", "absent", "equals", "not", "type", "contains"):
            if self.has(key):
                if key in ("exists", "absent"):
                    return f"{label} {key}"
                op = {"equals": "==", "not": "!="}.get(key, key)
                return f"{label} {op} {self.get(key)!r}"
        for key, symbol in _NUMERIC_OPS.items():
            if self.get(key) is not None:
                return f"{label} {symbol} {self.get(key)}"
        return label


@dataclass(slots=True)
class ResponseView:
    """The parts of a response assertions can look at."""

    status: int
    headers: dict[str, str]  # lowercase names
    text: str
    duration_ms: float
    _json: Any = _MISSING

    def json(self) -> Any:
        if self._json is _MISSING:
            try:
                self._json = json.loads(self.text)
            except ValueError:
                self._json = _MISSING
                return _MISSING
        return self._json


# ── Parsing ─────────────────────────────────────────────────────────────────


def _literal(text: str) -> Any:
    """A JSON literal when the text is one (``200``, ``true``, ``"x"``), else the text."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def _int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise AssertionSyntaxError(f"{what} must be a whole number")
    try:
        return int(value)
    except ValueError:
        raise AssertionSyntaxError(f"{what} must be a whole number") from None


def _status_codes(spec: str) -> list[int]:
    return [_int(part.strip(), "a status code") for part in spec.split(",") if part.strip()]


def _make(kind: str, **params: Any) -> Assertion:
    return Assertion(
        kind, tuple((k, v) for k, v in params.items() if v is not None or k in ("equals", "not"))
    )


def parse_assertion(item: Any) -> Assertion:
    """Normalize a compact line or a structured dict into an :class:`Assertion`."""
    if isinstance(item, Assertion):
        return item
    if isinstance(item, dict):
        return _from_dict(item)
    if isinstance(item, str):
        return _from_line(item)
    raise AssertionSyntaxError("an assertion must be a line of text or an object")


def parse_assertions(items: Any) -> list[Assertion]:
    if items is None:
        return []
    if not isinstance(items, (list, tuple)):
        raise AssertionSyntaxError("assertions must be a list")
    if len(items) > MAX_ASSERTIONS:
        raise AssertionSyntaxError(f"too many assertions (limit {MAX_ASSERTIONS})")
    return [parse_assertion(i) for i in items]


def _from_line(line: str) -> Assertion:
    text = line.strip()
    if not text:
        raise AssertionSyntaxError("an assertion is empty")
    kind, _, rest = text.partition(" ")
    kind = kind.lower()
    rest = rest.strip()
    if kind == "status":
        return _status_line(rest)
    if kind == "duration":
        match = re.fullmatch(r"(<=?)\s*(\d{1,7})\s*(?:ms)?", rest)
        if not match:
            raise AssertionSyntaxError("duration needs a limit, e.g. 'duration < 500'")
        limit = int(match.group(2))
        return _make("duration", max_ms=limit - 1 if match.group(1) == "<" else limit)
    if kind == "body":
        verb, _, value = rest.partition(" ")
        if verb not in ("contains", "not_contains") or not value:
            raise AssertionSyntaxError("body needs 'contains TEXT' or 'not_contains TEXT'")
        return _make("body", **{verb: value})
    if kind == "header":
        name, _, remainder = rest.partition(" ")
        if not name or not remainder.strip():
            raise AssertionSyntaxError("header needs a name and a check, e.g. 'header x exists'")
        return _value_check("header", {"name": name.lower()}, remainder.strip())
    if kind == "json":
        path, _, remainder = rest.partition(" ")
        if not path or not remainder.strip():
            raise AssertionSyntaxError("json needs a path and a check, e.g. 'json data.id exists'")
        _split_path(path)  # validates the path
        return _value_check("json", {"path": path}, remainder.strip())
    raise AssertionSyntaxError(
        f"unknown assertion '{kind[:20]}'; use status, header, json, body or duration"
    )


def _status_line(rest: str) -> Assertion:
    if rest.startswith("=="):
        return _make("status", equals=_int(rest[2:].strip(), "a status code"))
    if rest.startswith("!="):
        return _make("status", **{"not": _int(rest[2:].strip(), "a status code")})
    if rest.startswith("in "):
        spec = rest[3:].strip()
        match = re.fullmatch(r"(\d{3})\s*-\s*(\d{3})", spec)
        if match:
            low, high = int(match.group(1)), int(match.group(2))
            if low > high:
                raise AssertionSyntaxError("a status range must go from low to high")
            return _make("status", range=[low, high])
        return _make("status", **{"in": _status_codes(spec)})
    raise AssertionSyntaxError("status needs '== 200', '!= 500', 'in 200-299' or 'in 200,201'")


def _value_check(kind: str, base: dict[str, Any], remainder: str) -> Assertion:
    word, _, value = remainder.partition(" ")
    value = value.strip()
    if word in ("exists", "absent"):
        return _make(kind, **base, **{word: True})
    if word in ("==", "!="):
        if not value:
            raise AssertionSyntaxError(f"'{word}' needs a value")
        key = "equals" if word == "==" else "not"
        return _make(kind, **base, **{key: value if kind == "header" else _literal(value)})
    if word == "contains":
        if not value:
            raise AssertionSyntaxError("'contains' needs text")
        return _make(kind, **base, contains=value)
    if word == "type" and kind == "json":
        if value not in _TYPES:
            raise AssertionSyntaxError(f"type must be one of: {', '.join(_TYPES)}")
        return _make(kind, **base, type=value)
    if word in _SYMBOLS and kind == "json":
        try:
            number = float(value)
        except ValueError:
            raise AssertionSyntaxError(f"'{word}' needs a number") from None
        return _make(kind, **base, **{_SYMBOLS[word]: number})
    raise AssertionSyntaxError(f"unknown check '{word[:20]}'")


def _from_dict(item: dict[str, Any]) -> Assertion:
    kind = item.get("type")
    if kind not in ("status", "header", "json", "body", "duration"):
        raise AssertionSyntaxError(
            "an assertion object needs a 'type' of status/header/json/body/duration"
        )
    allowed = {
        "status": {"equals", "not", "in", "range"},
        "header": {"name", "exists", "absent", "equals", "not", "contains"},
        "json": {"path", "exists", "absent", "equals", "not", "type", "contains", *_NUMERIC_OPS},
        "body": {"contains", "not_contains"},
        "duration": {"max_ms"},
    }[kind]
    unknown = sorted(set(item) - allowed - {"type"})
    if unknown:
        raise AssertionSyntaxError(
            f"unknown field(s) for a {kind} assertion: {', '.join(unknown[:3])}"
        )
    params = {k: v for k, v in item.items() if k != "type"}
    if kind == "status":
        if "range" in params:
            rng = params["range"]
            if not (isinstance(rng, (list, tuple)) and len(rng) == 2):
                raise AssertionSyntaxError("a status range is [low, high]")
            params["range"] = [_int(rng[0], "a status code"), _int(rng[1], "a status code")]
        if "in" in params:
            if not isinstance(params["in"], (list, tuple)) or not params["in"]:
                raise AssertionSyntaxError("status 'in' needs a list of codes")
            params["in"] = [_int(c, "a status code") for c in params["in"]]
        for key in ("equals", "not"):
            if key in params:
                params[key] = _int(params[key], "a status code")
        if not params:
            raise AssertionSyntaxError("a status assertion needs a check")
    elif kind == "duration":
        params["max_ms"] = _int(params.get("max_ms"), "max_ms")
    elif kind == "header":
        if not isinstance(params.get("name"), str) or not params["name"]:
            raise AssertionSyntaxError("a header assertion needs a name")
        params["name"] = params["name"].lower()
    elif kind == "json":
        if not isinstance(params.get("path"), str):
            raise AssertionSyntaxError("a json assertion needs a path")
        _split_path(params["path"])
        if "type" in params and params["type"] not in _TYPES:
            raise AssertionSyntaxError(f"type must be one of: {', '.join(_TYPES)}")
    if kind in ("header", "json", "body") and len(params) < (2 if kind != "body" else 1):
        raise AssertionSyntaxError(f"a {kind} assertion needs a check")
    return Assertion(kind, tuple(sorted(params.items(), key=lambda kv: kv[0])))


# ── JSON paths ──────────────────────────────────────────────────────────────


def _split_path(path: str) -> list[str | int]:
    text = path[2:] if path.startswith("$.") else path.lstrip("$") if path == "$" else path
    if not text:
        return []
    steps: list[str | int] = []
    position = 0
    while position < len(text):
        match = _PATH_STEP.match(text, position)
        if not match or match.end() == position:
            raise AssertionSyntaxError(f"the JSON path '{path[:60]}' is not valid")
        if match.group(1) is not None:
            steps.append(match.group(1))
        elif match.group(2) is not None:
            steps.append(int(match.group(2)))
        else:
            steps.append(match.group(3))
        position = match.end()
        if len(steps) > 32:
            raise AssertionSyntaxError("the JSON path is too deep")
    return steps


def json_lookup(document: Any, path: str) -> Any:
    """Follow *path* in *document*; ``_MISSING`` (see :func:`is_missing`) when absent."""
    current = document
    for step in _split_path(path):
        if isinstance(step, int):
            if not isinstance(current, list) or step >= len(current):
                return _MISSING
            current = current[step]
        else:
            if not isinstance(current, dict) or step not in current:
                return _MISSING
            current = current[step]
    return current


def is_missing(value: Any) -> bool:
    return value is _MISSING


# ── Evaluation ──────────────────────────────────────────────────────────────


def _shown(value: Any) -> Any:
    if value is _MISSING:
        return "<missing>"
    if isinstance(value, str):
        return value[:_MAX_SHOWN]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    text = json.dumps(value, default=str)
    return text[:_MAX_SHOWN]


def _result(assertion: Assertion, passed: bool, expected: Any, actual: Any) -> dict[str, Any]:
    return {
        "assertion": assertion.describe(),
        "passed": bool(passed),
        "expected": _shown(expected),
        "actual": _shown(actual),
    }


def _same(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) != isinstance(expected, bool):
        return False
    return actual == expected


def evaluate_one(assertion: Assertion, view: ResponseView) -> dict[str, Any]:
    kind = assertion.kind
    if kind == "status":
        status = view.status
        if assertion.get("range"):
            low, high = assertion.get("range")
            return _result(assertion, low <= status <= high, f"{low}-{high}", status)
        if assertion.get("in") is not None:
            return _result(assertion, status in assertion.get("in"), assertion.get("in"), status)
        if assertion.get("not") is not None:
            return _result(
                assertion, status != assertion.get("not"), f"not {assertion.get('not')}", status
            )
        return _result(
            assertion, status == assertion.get("equals"), assertion.get("equals"), status
        )
    if kind == "duration":
        limit = assertion.get("max_ms")
        return _result(
            assertion, view.duration_ms <= limit, f"<= {limit} ms", round(view.duration_ms, 1)
        )
    if kind == "body":
        if assertion.get("contains") is not None:
            needle = assertion.get("contains")
            return _result(
                assertion,
                needle in view.text,
                f"contains {needle!r}",
                f"{len(view.text)} characters",
            )
        needle = assertion.get("not_contains")
        return _result(
            assertion,
            needle not in view.text,
            f"without {needle!r}",
            f"{len(view.text)} characters",
        )
    if kind == "header":
        actual = view.headers.get(assertion.get("name"), _MISSING)
        return _evaluate_value(assertion, actual, header=True)
    document = view.json()
    if document is _MISSING:
        return _result(assertion, False, "a JSON body", "the body is not valid JSON")
    return _evaluate_value(assertion, json_lookup(document, assertion.get("path")), header=False)


def _evaluate_value(assertion: Assertion, actual: Any, *, header: bool) -> dict[str, Any]:
    present = actual is not _MISSING
    if assertion.get("exists"):
        return _result(assertion, present, "present", actual if present else _MISSING)
    if assertion.get("absent"):
        return _result(assertion, not present, "absent", actual if present else "absent")
    if not present:
        expected = next(
            (assertion.get(k) for k in ("equals", "not", "type", "contains") if assertion.has(k)),
            None,
        )
        return _result(assertion, False, expected, _MISSING)
    if assertion.has("equals"):
        expected = assertion.get("equals")
        return _result(assertion, _same(actual, expected), expected, actual)
    if assertion.has("not"):
        expected = assertion.get("not")
        return _result(assertion, not _same(actual, expected), f"not {expected!r}", actual)
    if assertion.get("type") is not None:
        wanted = assertion.get("type")
        python_type = _TYPES[wanted]
        ok = isinstance(actual, python_type)
        if wanted in ("number", "integer") and isinstance(actual, bool):
            ok = False
        return _result(assertion, ok, wanted, type(actual).__name__)
    if assertion.get("contains") is not None:
        needle = assertion.get("contains")
        if isinstance(actual, str):
            ok = needle in actual
        elif isinstance(actual, list):
            ok = any(_same(item, _literal(str(needle))) or item == needle for item in actual)
        else:
            ok = False
        return _result(assertion, ok, f"contains {needle!r}", actual)
    for key, symbol in _NUMERIC_OPS.items():
        bound = assertion.get(key)
        if bound is not None:
            if isinstance(actual, bool) or not isinstance(actual, (int, float)):
                return _result(assertion, False, f"{symbol} {bound}", actual)
            ok = {
                "gt": actual > bound,
                "gte": actual >= bound,
                "lt": actual < bound,
                "lte": actual <= bound,
            }[key]
            return _result(assertion, ok, f"{symbol} {bound}", actual)
    return _result(assertion, False, "a check", actual)


def evaluate(assertions: list[Assertion], view: ResponseView) -> dict[str, Any]:
    """Evaluate every assertion; the summary is machine-readable and complete."""
    results = [evaluate_one(a, view) for a in assertions]
    failed = [r for r in results if not r["passed"]]
    return {
        "passed": not failed,
        "total": len(results),
        "failed_count": len(failed),
        "results": results,
    }
