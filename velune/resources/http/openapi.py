"""OpenAPI 3.x support: read a specification, find endpoints, check requests (VAIR A3).

**A specification is untrusted input.** It is parsed with a size cap, JSON first
and YAML through ``yaml.safe_load`` (the optional ``[api]`` extra), and it is
only ever *read*:

* Only local ``#/…`` references are followed, to a bounded depth and a bounded
  number of expansions, so a self-referencing or exponentially expanding document
  cannot hang or exhaust memory. External references are not fetched.
* Nothing in a spec is executed or trusted: ``servers`` entries are *suggestions*
  that go through the same network policy as any URL, and security schemes only
  describe what an endpoint wants — credentials still come from a named
  credential reference bound to an origin.
* Endpoints are **not** turned into tools. A catalog of hundreds of operations
  would blow the model's tool budget, so the model searches the catalog and calls
  endpoints through one ``openapi_call`` operation.

Supported: OpenAPI 3.0.x and 3.1.x (Swagger 2.0 is rejected with a clear message),
path/query/header parameters, JSON/form request bodies, response summaries and
security requirements. Request validation covers the common JSON-Schema subset
(type, required, enum, properties, items, numeric and length bounds); it does not
implement ``oneOf``/``anyOf``/``allOf`` semantics, regex ``pattern`` or formats.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

MAX_SPEC_BYTES = 5 * 1024 * 1024
MAX_ENDPOINTS = 5000
MAX_REF_DEPTH = 12
MAX_EXPANSIONS = 250_000  # reference expansions, not plain nodes
_METHODS = ("get", "put", "post", "delete", "options", "head", "patch")
_LOCATIONS = ("path", "query", "header")
_SLUG = re.compile(r"[^a-zA-Z0-9]+")
_PATH_TOKEN = re.compile(r"\{([A-Za-z0-9_.\-]+)\}")
_SERVER_VAR = re.compile(r"\{([A-Za-z0-9_.\-]+)\}")


class OpenApiError(ValueError):
    """A specification that cannot be used (message is safe to show the model)."""


@dataclass(frozen=True, slots=True)
class Parameter:
    name: str
    location: str
    required: bool
    schema: dict[str, Any]
    description: str = ""


@dataclass(frozen=True, slots=True)
class Endpoint:
    operation_id: str
    method: str
    path: str
    summary: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    parameters: tuple[Parameter, ...] = ()
    body_schema: dict[str, Any] | None = None
    body_content_type: str = ""
    body_required: bool = False
    responses: dict[str, str] = field(default_factory=dict)
    security: tuple[str, ...] = ()
    deprecated: bool = False


@dataclass(slots=True)
class ApiCatalog:
    name: str
    title: str
    version: str
    servers: list[str]
    endpoints: dict[str, Endpoint]
    security_schemes: dict[str, dict[str, str]]
    source: str
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        methods: dict[str, int] = {}
        for e in self.endpoints.values():
            methods[e.method] = methods.get(e.method, 0) + 1
        return {
            "name": self.name,
            "title": self.title,
            "version": self.version,
            "endpoints": len(self.endpoints),
            "methods": dict(sorted(methods.items())),
            "servers": self.servers[:5],
            "security_schemes": {k: v.get("type", "") for k, v in self.security_schemes.items()},
            "tags": sorted({t for e in self.endpoints.values() for t in e.tags})[:30],
            **({"warnings": self.warnings[:5]} if self.warnings else {}),
        }


def load_document(text: str) -> dict[str, Any]:
    """Parse JSON, then YAML (``yaml.safe_load``) when it is not JSON."""
    if len(text.encode("utf-8", "ignore")) > MAX_SPEC_BYTES:
        raise OpenApiError(
            f"the specification is larger than {MAX_SPEC_BYTES // (1024 * 1024)} MiB"
        )
    stripped = text.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            document = json.loads(text)
        except ValueError:
            raise OpenApiError("the specification is not valid JSON") from None
    else:
        try:
            import yaml
        except ImportError:
            raise OpenApiError(
                "this looks like YAML; install it with: pip install 'velune-cli[api]' "
                "(or provide the specification as JSON)"
            ) from None
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError:
            raise OpenApiError("the specification is not valid YAML") from None
    if not isinstance(document, dict):
        raise OpenApiError("the specification must be an object")
    return document


class _Resolver:
    """Follow local ``$ref`` pointers with depth and expansion limits."""

    def __init__(self, document: dict[str, Any]) -> None:
        self._document = document
        self._expansions = 0
        self.warnings: list[str] = []

    def _lookup(self, ref: str) -> Any:
        if not ref.startswith("#/"):
            if "external references are not followed" not in self.warnings:
                self.warnings.append("external references are not followed")
            return None
        node: Any = self._document
        for raw in ref[2:].split("/"):
            key = raw.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and key in node:
                node = node[key]
            elif isinstance(node, list) and key.isdigit() and int(key) < len(node):
                node = node[int(key)]
            else:
                return None
        return node

    def resolve(self, node: Any, depth: int = 0, seen: tuple[str, ...] = ()) -> Any:
        """A copy of *node* with references expanded, bounded and cycle-safe."""
        if depth > MAX_REF_DEPTH:
            return {"description": "(nested too deeply)"}
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                if ref in seen:
                    return {"description": f"(recursive: {ref.rsplit('/', 1)[-1]})"}
                self._expansions += 1
                if self._expansions > MAX_EXPANSIONS:
                    raise OpenApiError("the specification expands to too many references")
                target = self._lookup(ref)
                if target is None:
                    return {"description": "(unresolved reference)"}
                return self.resolve(target, depth + 1, (*seen, ref))
            return {k: self.resolve(v, depth + 1, seen) for k, v in node.items() if k != "$ref"}
        if isinstance(node, list):
            return [self.resolve(v, depth + 1, seen) for v in node[:200]]
        return node


def _short(text: Any, limit: int = 200) -> str:
    return str(text or "").strip().replace("\n", " ")[:limit]


def _schema_view(schema: Any, depth: int = 0) -> Any:
    """A compact, model-readable summary of a (resolved) schema."""
    if not isinstance(schema, dict) or depth > 3:
        return {}
    view: dict[str, Any] = {}
    for key in (
        "type",
        "format",
        "enum",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "description",
        "default",
    ):
        if key in schema:
            value = schema[key]
            view[key] = _short(value) if key == "description" else value
    if isinstance(schema.get("properties"), dict):
        view["properties"] = {
            name: _schema_view(sub, depth + 1)
            for name, sub in list(schema["properties"].items())[:40]
        }
    if isinstance(schema.get("required"), list):
        view["required"] = [r for r in schema["required"] if isinstance(r, str)][:40]
    if isinstance(schema.get("items"), dict):
        view["items"] = _schema_view(schema["items"], depth + 1)
    for combinator in ("oneOf", "anyOf", "allOf"):
        if isinstance(schema.get(combinator), list):
            view[combinator] = f"{len(schema[combinator])} alternatives (not validated)"
    return view


def parse_spec(text: str, *, name: str, source: str) -> ApiCatalog:
    """Parse *text* into an :class:`ApiCatalog` (raises :class:`OpenApiError`)."""
    document = load_document(text)
    version = str(document.get("openapi", ""))
    if document.get("swagger"):
        raise OpenApiError("Swagger 2.0 is not supported; convert the specification to OpenAPI 3.x")
    if not version.startswith("3."):
        raise OpenApiError("this is not an OpenAPI 3.x document (no 'openapi: 3.x' field)")
    paths = document.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise OpenApiError("the specification has no 'paths'")
    resolver = _Resolver(document)
    info = document.get("info") if isinstance(document.get("info"), dict) else {}
    endpoints: dict[str, Endpoint] = {}
    for path, item in paths.items():
        if not isinstance(path, str) or not path.startswith("/") or not isinstance(item, dict):
            continue
        item = resolver.resolve(item)
        shared = item.get("parameters", [])
        for method in _METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            if len(endpoints) >= MAX_ENDPOINTS:
                resolver.warnings.append(f"only the first {MAX_ENDPOINTS} endpoints were read")
                break
            endpoint = _endpoint(resolver, path, method, op, shared, document)
            base_id, counter = endpoint.operation_id, 2
            while endpoint.operation_id in endpoints:
                endpoint = _replace_id(endpoint, f"{base_id}_{counter}")
                counter += 1
            endpoints[endpoint.operation_id] = endpoint
    if not endpoints:
        raise OpenApiError("the specification declares no operations")
    components = document.get("components") if isinstance(document.get("components"), dict) else {}
    schemes = {}
    for scheme_name, scheme in (components.get("securitySchemes") or {}).items():
        scheme = resolver.resolve(scheme)
        if isinstance(scheme, dict):
            schemes[str(scheme_name)] = {
                k: _short(scheme.get(k), 60)
                for k in ("type", "scheme", "in", "name")
                if scheme.get(k)
            }
    return ApiCatalog(
        name=name,
        title=_short(info.get("title"), 120),
        version=_short(info.get("version"), 40),
        servers=_servers(document),
        endpoints=endpoints,
        security_schemes=schemes,
        source=source,
        warnings=resolver.warnings,
    )


def _replace_id(endpoint: Endpoint, operation_id: str) -> Endpoint:
    from dataclasses import replace

    return replace(endpoint, operation_id=operation_id)


def _servers(document: dict[str, Any]) -> list[str]:
    urls = []
    for server in document.get("servers") or []:
        if not isinstance(server, dict) or not isinstance(server.get("url"), str):
            continue
        url = server["url"]
        variables = server.get("variables") if isinstance(server.get("variables"), dict) else {}

        urls.append(_fill_server(url, variables)[:300])
    return urls


def _fill_server(url: str, variables: dict[str, Any]) -> str:
    def fill(match: re.Match[str]) -> str:
        spec = variables.get(match.group(1))
        return str(spec.get("default", "")) if isinstance(spec, dict) else match.group(0)

    return _SERVER_VAR.sub(fill, url)


def _operation_id(op: dict[str, Any], method: str, path: str) -> str:
    raw = op.get("operationId")
    if isinstance(raw, str) and raw.strip():
        slug = _SLUG.sub("_", raw.strip()).strip("_")
    else:
        slug = _SLUG.sub("_", f"{method}_{path}").strip("_").lower()
    return (slug or f"{method}_op")[:64]


def _endpoint(
    resolver: _Resolver,
    path: str,
    method: str,
    op: dict[str, Any],
    shared: list[Any],
    document: dict[str, Any],
) -> Endpoint:
    op = resolver.resolve(op)
    params: dict[tuple[str, str], Parameter] = {}
    for raw in [*shared, *(op.get("parameters") or [])]:
        raw = resolver.resolve(raw)
        if not isinstance(raw, dict) or raw.get("in") not in _LOCATIONS or not raw.get("name"):
            continue
        location = raw["in"]
        schema = _schema_view(raw.get("schema") or {})
        params[(location, str(raw["name"]))] = Parameter(
            name=str(raw["name"])[:80],
            location=location,
            required=bool(raw.get("required")) or location == "path",
            schema=schema,
            description=_short(raw.get("description")),
        )
    for token in _PATH_TOKEN.findall(path):  # a path variable the spec forgot to declare
        params.setdefault(("path", token), Parameter(token, "path", True, {"type": "string"}))
    body_schema, content_type, body_required = None, "", False
    request_body = resolver.resolve(op.get("requestBody")) if op.get("requestBody") else None
    if isinstance(request_body, dict):
        body_required = bool(request_body.get("required"))
        content = (
            request_body.get("content") if isinstance(request_body.get("content"), dict) else {}
        )
        keys = list(content)
        chosen = next((k for k in keys if "json" in k.lower()), None) or next(
            (
                k
                for k in keys
                if k.lower() in ("application/x-www-form-urlencoded", "multipart/form-data")
            ),
            keys[0] if keys else "",
        )
        if chosen:
            content_type = chosen
            entry = content.get(chosen)
            body_schema = entry.get("schema") if isinstance(entry, dict) else None
    responses = {}
    for status, response in (op.get("responses") or {}).items():
        response = resolver.resolve(response)
        description = _short(response.get("description") if isinstance(response, dict) else "", 120)
        responses[str(status)] = description
    security: list[str] = []
    for requirement in op.get("security", document.get("security", [])) or []:
        if isinstance(requirement, dict):
            security.extend(str(k) for k in requirement)
    return Endpoint(
        operation_id=_operation_id(op, method, path),
        method=method.upper(),
        path=path,
        summary=_short(op.get("summary"), 160),
        description=_short(op.get("description"), 300),
        tags=tuple(str(t)[:40] for t in (op.get("tags") or [])[:5]),
        parameters=tuple(params.values()),
        body_schema=_schema_view(body_schema) if body_schema else None,
        body_content_type=content_type,
        body_required=body_required,
        responses=dict(list(responses.items())[:12]),
        security=tuple(dict.fromkeys(security)),
        deprecated=bool(op.get("deprecated")),
    )


# ── Searching ───────────────────────────────────────────────────────────────

_WORD = re.compile(r"[a-z0-9]+")


def search(
    catalog: ApiCatalog,
    query: str = "",
    *,
    tag: str = "",
    method: str = "",
    limit: int = 15,
) -> list[Endpoint]:
    """Endpoints matching *query* (whole-word hits, deterministic order)."""
    words = _WORD.findall(query.lower())
    scored: list[tuple[int, str, Endpoint]] = []
    for endpoint in catalog.endpoints.values():
        if method and endpoint.method != method.upper():
            continue
        if tag and tag.lower() not in {t.lower() for t in endpoint.tags}:
            continue
        haystack = f"{endpoint.operation_id} {endpoint.path} {endpoint.summary} {endpoint.description} {' '.join(endpoint.tags)}".lower()
        tokens = set(_WORD.findall(haystack))
        score = sum(
            2 if w in _WORD.findall(endpoint.operation_id.lower()) else 1
            for w in words
            if w in tokens
        )
        if words and score == 0:
            continue
        scored.append((-score, f"{endpoint.path}{endpoint.method}", endpoint))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [e for _, _, e in scored[: max(1, min(limit, 50))]]


def endpoint_card(endpoint: Endpoint) -> dict[str, Any]:
    """A short entry for a search listing."""
    card: dict[str, Any] = {
        "operation": endpoint.operation_id,
        "method": endpoint.method,
        "path": endpoint.path,
    }
    if endpoint.summary:
        card["summary"] = endpoint.summary
    if endpoint.tags:
        card["tags"] = list(endpoint.tags)
    if endpoint.deprecated:
        card["deprecated"] = True
    return card


def endpoint_detail(endpoint: Endpoint, catalog: ApiCatalog) -> dict[str, Any]:
    """Everything needed to call an endpoint correctly."""
    detail = endpoint_card(endpoint)
    if endpoint.description:
        detail["description"] = endpoint.description
    detail["parameters"] = [
        {
            "name": p.name,
            "in": p.location,
            "required": p.required,
            **({"schema": p.schema} if p.schema else {}),
            **({"description": p.description} if p.description else {}),
        }
        for p in endpoint.parameters
    ]
    if endpoint.body_schema is not None or endpoint.body_content_type:
        detail["request_body"] = {
            "content_type": endpoint.body_content_type,
            "required": endpoint.body_required,
            **({"schema": endpoint.body_schema} if endpoint.body_schema else {}),
        }
    if endpoint.responses:
        detail["responses"] = endpoint.responses
    if endpoint.security:
        detail["security"] = [
            {"name": s, **catalog.security_schemes.get(s, {})} for s in endpoint.security
        ]
    return detail


# ── Validating a request against the spec ───────────────────────────────────

_PY_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def validate_value(
    schema: dict[str, Any], value: Any, where: str, errors: list[str], depth: int = 0
) -> None:
    """Check *value* against the supported schema subset; append problems to *errors*.

    Messages name the location and the rule, never the offending value.
    """
    if len(errors) >= 10 or depth > 8 or not isinstance(schema, dict):
        return
    kind = schema.get("type")
    if isinstance(kind, str) and kind in _PY_TYPES:
        ok = isinstance(value, _PY_TYPES[kind]) and not (
            kind in ("integer", "number") and isinstance(value, bool)
        )
        if not ok:
            errors.append(f"{where}: expected {kind}")
            return
    if "enum" in schema and isinstance(schema["enum"], list) and value not in schema["enum"]:
        errors.append(f"{where}: not one of the allowed values")
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(schema.get("minimum"), (int, float)) and value < schema["minimum"]:
            errors.append(f"{where}: below the minimum {schema['minimum']}")
        if isinstance(schema.get("maximum"), (int, float)) and value > schema["maximum"]:
            errors.append(f"{where}: above the maximum {schema['maximum']}")
    if isinstance(value, str):
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            errors.append(f"{where}: shorter than {schema['minLength']} characters")
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            errors.append(f"{where}: longer than {schema['maxLength']} characters")
    if isinstance(value, dict):
        for required in schema.get("required", []) or []:
            if required not in value:
                errors.append(f"{where}.{required}: required")
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                validate_value(sub, value[key], f"{where}.{key}", errors, depth + 1)
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value[:50]):
            validate_value(schema["items"], item, f"{where}[{index}]", errors, depth + 1)


def coerce_parameter(schema: dict[str, Any], text: str) -> Any:
    """Convert a textual parameter to the type its schema declares (else leave text)."""
    kind = schema.get("type")
    try:
        if kind == "integer":
            return int(text)
        if kind == "number":
            return float(text)
        if kind == "boolean":
            return {"true": True, "false": False}[text.lower()]
    except (ValueError, KeyError):
        return text
    return text


def check_call(
    endpoint: Endpoint,
    *,
    path: dict[str, str],
    query: dict[str, str],
    headers: dict[str, str],
    body: Any,
    has_body: bool,
) -> list[str]:
    """Problems with a call to *endpoint* (empty when it is consistent with the spec)."""
    errors: list[str] = []
    groups = {"path": path, "query": query, "header": {k.lower(): v for k, v in headers.items()}}
    for param in endpoint.parameters:
        supplied = groups[param.location]
        key = param.name.lower() if param.location == "header" else param.name
        if key not in supplied:
            if param.required:
                errors.append(f"{param.location} parameter '{param.name}' is required")
            continue
        validate_value(
            param.schema,
            coerce_parameter(param.schema, supplied[key]),
            f"{param.location} '{param.name}'",
            errors,
        )
    if endpoint.body_required and not has_body:
        errors.append("a request body is required")
    if has_body and endpoint.body_schema:
        validate_value(endpoint.body_schema, body, "body", errors)
    return errors[:10]


def fill_path(template: str, values: dict[str, str]) -> str:
    """Substitute ``{name}`` path variables, percent-encoding each value."""
    from urllib.parse import quote

    def sub(match: re.Match[str]) -> str:
        return quote(values[match.group(1)], safe="")

    return _PATH_TOKEN.sub(sub, template)
