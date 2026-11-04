"""Strict, dependency-free checking of tool arguments against the tools' own declared schemas.

Every read-only tool declares a flat JSON-Schema object (``type``, ``properties``, ``required``,
``minimum``). Rather than add ``jsonschema`` (installed only transitively, not a declared
dependency) this module enforces exactly the subset those schemas use, and treats everything else
as *unenforceable*: a tool whose schema uses a construct this module does not understand is refused
by policy instead of being offered with unchecked arguments.

Differences from a general JSON-Schema validator, all in the fail-closed direction:

* undeclared argument names are rejected (JSON Schema would allow them by default);
* ``null`` is never accepted, and ``bool`` is never accepted as a number;
* ``integer`` means a real ``int``; ``1.0`` is not an integer;
* values are bounded in depth, node count, string length and total encoded size.

Error messages name the argument and the rule broken, never the offending value, so a secret placed
in a bad argument is not echoed into logs or artifacts.
"""

from __future__ import annotations

import json
import re
from typing import Any

MAX_DEPTH = 6  # the arguments object is depth 1
MAX_NODES = 256
MAX_KEYS = 32
MAX_ARRAY_ITEMS = 64
MAX_STRING_CHARS = 4096
MAX_ARGUMENT_BYTES = 16 * 1024
MAX_SAFE_INT = 2**53

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

_ROOT_KEYWORDS = frozenset(
    {"type", "properties", "required", "description", "title", "additionalProperties"}
)
_PROPERTY_KEYWORDS = frozenset(
    {
        "type",
        "description",
        "title",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "enum",
        "default",
        "items",
        "minItems",
        "maxItems",
    }
)
_SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean"})


class ArgumentError(ValueError):
    """Arguments the trusted validator will not accept. ``kind`` is a stable identifier."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


# ── bounded canonical JSON ───────────────────────────────────────────────────


def canonical_arguments(arguments: Any) -> str:
    """Canonical JSON for *arguments*, or ``ArgumentError`` if they are not safe JSON.

    Accepts only exact ``dict`` / ``list`` / ``str`` / ``int`` / ``float`` / ``bool`` / ``None``
    (subclasses, tuples, sets, bytes, paths and arbitrary objects are rejected) with string keys.
    The walk is iterative and depth-capped, so a hostile or self-referential structure cannot
    exhaust the stack. Key order never changes the result: equal objects give equal bytes.
    """
    if type(arguments) is not dict:
        raise ArgumentError("not_json", "arguments must be a JSON object")
    stack: list[tuple[Any, int]] = [(arguments, 1)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > MAX_NODES:
            raise ArgumentError("too_large", f"arguments have more than {MAX_NODES} values")
        if depth > MAX_DEPTH:
            raise ArgumentError("too_deep", f"arguments nest deeper than {MAX_DEPTH} levels")
        kind = type(value)
        if kind is dict:
            if len(value) > MAX_KEYS:
                raise ArgumentError("too_large", f"an object has more than {MAX_KEYS} keys")
            for key, item in value.items():
                if type(key) is not str or not _KEY.match(key):
                    raise ArgumentError("not_json", "argument names must be simple identifiers")
                stack.append((item, depth + 1))
        elif kind is list:
            if len(value) > MAX_ARRAY_ITEMS:
                raise ArgumentError("too_large", f"a list has more than {MAX_ARRAY_ITEMS} items")
            stack.extend((item, depth + 1) for item in value)
        elif kind is str:
            _check_string(value)
        elif kind is bool or value is None:
            pass
        elif kind is int:
            if abs(value) > MAX_SAFE_INT:
                raise ArgumentError("not_json", "an integer is outside the safe range")
        elif kind is float:
            if value != value or value in (float("inf"), float("-inf")):
                raise ArgumentError("not_json", "NaN and infinity are not valid JSON")
        else:
            raise ArgumentError("not_json", f"a {kind.__name__} value is not JSON-compatible")
    text = json.dumps(arguments, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(text.encode("utf-8")) > MAX_ARGUMENT_BYTES:
        raise ArgumentError("too_large", f"arguments exceed {MAX_ARGUMENT_BYTES} bytes")
    return text


def _check_string(value: str) -> None:
    if len(value) > MAX_STRING_CHARS:
        raise ArgumentError("too_large", f"a string is longer than {MAX_STRING_CHARS} characters")
    if "\x00" in value:
        raise ArgumentError("not_json", "a string contains a NUL character")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ArgumentError("not_json", "a string is not valid Unicode") from None


# ── which schemas can be enforced ────────────────────────────────────────────


def schema_problem(schema: Any) -> str | None:
    """Why this schema cannot be enforced strictly, or ``None`` if it can."""
    if not isinstance(schema, dict):
        return "the schema is not an object"
    if not schema:
        return None  # declares no parameters: only an empty arguments object is acceptable
    unknown = set(schema) - _ROOT_KEYWORDS
    if unknown:
        return f"unsupported schema keyword {sorted(unknown)[0]!r}"
    if schema.get("type") != "object":
        return "the root schema is not type 'object'"
    if schema.get("additionalProperties", False) is not False:
        return "additionalProperties must be false or absent"
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return "'properties' is not an object"
    for name, spec in properties.items():
        if not isinstance(name, str) or not _KEY.match(name):
            return "a property name is not a simple identifier"
        problem = _property_problem(spec, top=True)
        if problem:
            return f"property {name!r}: {problem}"
    required = schema.get("required", [])
    if not isinstance(required, list) or any(item not in properties for item in required):
        return "'required' names a property that is not declared"
    return None


def _property_problem(spec: Any, *, top: bool) -> str | None:
    if not isinstance(spec, dict):
        return "the property schema is not an object"
    unknown = set(spec) - _PROPERTY_KEYWORDS
    if unknown:
        return f"unsupported keyword {sorted(unknown)[0]!r}"
    kind = spec.get("type")
    if kind in _SCALAR_TYPES:
        return None
    if kind == "array":
        items = spec.get("items")
        if items is None:
            return "an array needs an 'items' schema"
        if not top:
            return "arrays nested in arrays are not supported"
        return _property_problem(items, top=False)
    return f"unsupported type {kind!r}"


# ── checking arguments against a supported schema ────────────────────────────


def check_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
    """Raise ``ArgumentError('schema', ...)`` unless *arguments* satisfy *schema*.

    *arguments* must already have passed :func:`canonical_arguments`. The schema must be one
    :func:`schema_problem` accepts; anything else is refused here too.
    """
    problem = schema_problem(schema)
    if problem:
        raise ArgumentError("schema", f"the tool's schema cannot be enforced: {problem}")
    properties: dict[str, Any] = schema.get("properties", {})
    for name in arguments:
        if name not in properties:
            raise ArgumentError("schema", f"unknown argument {name!r}")
    for name in schema.get("required", []):
        if name not in arguments:
            raise ArgumentError("schema", f"missing required argument {name!r}")
    for name, value in arguments.items():
        _check_value(name, properties[name], value)


def _check_value(label: str, spec: dict[str, Any], value: Any) -> None:
    kind = spec["type"]
    if kind == "array":
        if type(value) is not list:
            raise ArgumentError("schema", f"{label!r} must be a list")
        _bound(label, spec, len(value), "minItems", "maxItems", "items")
        for item in value:
            _check_value(f"{label}[]", spec["items"], item)
        return
    if kind == "string":
        ok = type(value) is str
    elif kind == "integer":
        ok = type(value) is int
    elif kind == "number":
        ok = type(value) in (int, float)
    else:  # boolean
        ok = type(value) is bool
    if not ok:
        raise ArgumentError("schema", f"{label!r} must be of type {kind}")
    if kind == "string":
        _bound(label, spec, len(value), "minLength", "maxLength", "characters")
    if kind in ("integer", "number"):
        if "minimum" in spec and value < spec["minimum"]:
            raise ArgumentError("schema", f"{label!r} is below the minimum of {spec['minimum']}")
        if "maximum" in spec and value > spec["maximum"]:
            raise ArgumentError("schema", f"{label!r} is above the maximum of {spec['maximum']}")
    if "enum" in spec and value not in spec["enum"]:
        raise ArgumentError("schema", f"{label!r} is not one of the allowed values")


def _bound(label: str, spec: dict[str, Any], size: int, low: str, high: str, unit: str) -> None:
    if low in spec and size < spec[low]:
        raise ArgumentError("schema", f"{label!r} has fewer than {spec[low]} {unit}")
    if high in spec and size > spec[high]:
        raise ArgumentError("schema", f"{label!r} has more than {spec[high]} {unit}")
