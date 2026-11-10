"""API collections: ordered, replayable groups of requests (VAIR A3).

A collection is a JSON document Velune reads and writes itself — no Postman
desktop application or cloud service is involved. Format ``velune-api-collection/1``::

    {
      "format": "velune-api-collection/1",
      "name": "aidly-auth",
      "description": "Sign-up and login checks",
      "environment": "development",          // optional default environment
      "auth": "aidly-dev-token",             // optional default credential reference
      "headers": {"Accept": "application/json"},
      "variables": {"email": "demo@example.com"},
      "requests": [
        {"id": "login", "name": "Log in", "method": "POST",
         "url": "{{base_url}}/auth/login",
         "body": {"json": {"email": "{{email}}", "password": "{{demo_password}}"}},
         "assertions": ["status == 200", "json data.token exists"],
         "extract": [{"var": "token", "json": "data.token", "secret": true}]}
      ]
    }

Rules:

* **No secrets in the file.** A header such as ``Authorization`` or ``Cookie``
  with a literal value is rejected: use ``"auth": "<credential reference>"`` or a
  ``{{secret_variable}}``. Values the file *extracts* at run time and marks
  ``secret`` live only in memory for that run.
* Everything in a collection is **untrusted data**. A collection cannot grant
  trust, add origins or bypass approval: running one is authorized against *all*
  of its requests together (see the connector), and each request is then
  checked by the network policy again when it connects.
* Assertions use the same compact lines or objects as ad-hoc requests.

Postman Collection v2.1 import/export is a **scoped subset**: requests (method,
URL, headers, raw / urlencoded / form-data text bodies), folders (flattened) and
collection variables. Scripts, tests, pre-request logic, auth blocks and
environments are *not* imported; the importer lists what it skipped.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from velune.resources.http.assertions import (
    Assertion,
    AssertionSyntaxError,
    parse_assertion,
)
from velune.resources.http.auth import valid_reference
from velune.resources.http.client import FORBIDDEN_HEADERS, METHODS

FORMAT = "velune-api-collection/1"
POSTMAN_SCHEMA = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
MAX_FILE_BYTES = 1024 * 1024
MAX_STRING = 8192
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
#: Headers that carry credentials: never stored as literals in a collection file.
CREDENTIAL_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "x-api-key",
        "api-key",
        "x-auth-token",
        "x-access-token",
    }
)
_BODY_KINDS = ("json", "form", "text", "multipart")
_COLLECTION_KEYS = frozenset(
    {"format", "name", "description", "environment", "auth", "headers", "variables", "requests"}
)
_REQUEST_KEYS = frozenset(
    {
        "id",
        "name",
        "method",
        "url",
        "headers",
        "query",
        "body",
        "auth",
        "assertions",
        "extract",
        "timeout",
        "description",
    }
)


class CollectionError(ValueError):
    """A malformed collection (message is safe to show the model)."""


@dataclass(frozen=True, slots=True)
class CollectionRequest:
    id: str
    name: str
    method: str
    url: str
    headers: tuple[tuple[str, str], ...] = ()
    query: tuple[tuple[str, str], ...] = ()
    body: dict[str, Any] | None = None
    auth: str | None = None  # a credential reference, or "none" to send no credentials
    assertions: tuple[Assertion, ...] = ()
    extract: tuple[dict[str, Any], ...] = ()
    timeout: int | None = None
    description: str = ""


@dataclass(frozen=True, slots=True)
class Collection:
    name: str
    description: str = ""
    environment: str | None = None
    auth: str | None = None
    headers: tuple[tuple[str, str], ...] = ()
    variables: tuple[tuple[str, str], ...] = ()
    requests: tuple[CollectionRequest, ...] = ()

    def request(self, request_id: str) -> CollectionRequest | None:
        return next((r for r in self.requests if r.id == request_id), None)


def valid_name(name: Any) -> bool:
    return isinstance(name, str) and bool(_NAME_RE.match(name))


def slugify(text: str, fallback: str = "request") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48].strip("-")
    return slug if slug and slug[0].isalnum() else fallback


def _text(value: Any, what: str, limit: int = MAX_STRING) -> str:
    if not isinstance(value, str):
        raise CollectionError(f"{what} must be text")
    if len(value) > limit:
        raise CollectionError(f"{what} is too long")
    return value


def _pairs(value: Any, what: str, *, headers: bool = False) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if isinstance(value, dict):
        items = list(value.items())
    elif isinstance(value, list):
        items = []
        for entry in value:
            if not (isinstance(entry, (list, tuple)) and len(entry) == 2):
                raise CollectionError(f"{what} must be an object of names to values")
            items.append((entry[0], entry[1]))
    else:
        raise CollectionError(f"{what} must be an object of names to values")
    if len(items) > 64:
        raise CollectionError(f"{what} has too many entries")
    out = []
    for key, val in items:
        key = _text(key, f"a name in {what}", 128)
        val = _text(val if isinstance(val, str) else str(val), f"the value of '{key[:30]}'")
        if headers:
            lowered = key.lower()
            if lowered in FORBIDDEN_HEADERS:
                raise CollectionError(
                    f"the '{key}' header is set by the client and cannot be listed"
                )
            if lowered in CREDENTIAL_HEADERS and "{{" not in val:
                raise CollectionError(
                    f'the \'{key}\' header holds a literal credential. Use "auth": "<credential '
                    'reference>" or a {{secret_variable}} instead of storing it in the file'
                )
        out.append((key, val))
    return tuple(out)


def _body(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or len([k for k in value if k in _BODY_KINDS]) != 1:
        raise CollectionError("a body must have exactly one of: json, form, text, multipart")
    kind = next(k for k in value if k in _BODY_KINDS)
    extra = set(value) - {kind, "content_type"}
    if extra:
        raise CollectionError(f"unknown body field(s): {', '.join(sorted(extra)[:3])}")
    if kind == "json":
        if len(json.dumps(value["json"])) > 200_000:
            raise CollectionError("the JSON body is too large")
        return {"json": value["json"]}
    if kind in ("form", "multipart"):
        fields = value[kind]
        if not isinstance(fields, dict) or not all(isinstance(v, str) for v in fields.values()):
            raise CollectionError(f"a {kind} body is an object of text values")
        return {kind: dict(fields)}
    text = _text(value["text"], "the text body", 200_000)
    content_type = _text(value.get("content_type", ""), "content_type", 200)
    if not content_type:
        raise CollectionError("a text body needs a content_type")
    return {"text": text, "content_type": content_type}


def _extract(value: Any) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 20:
        raise CollectionError("extract must be a short list")
    out = []
    for item in value:
        if not isinstance(item, dict) or not _VAR_NAME.match(str(item.get("var", ""))):
            raise CollectionError("each extract rule needs a valid 'var' name")
        sources = [k for k in ("json", "header") if k in item]
        if len(sources) != 1:
            raise CollectionError("an extract rule needs exactly one of 'json' or 'header'")
        if sources[0] == "json":
            try:
                parse_assertion({"type": "json", "path": item["json"], "exists": True})
            except AssertionSyntaxError as exc:
                raise CollectionError(f"extract '{item['var']}': {exc}") from None
        out.append(
            {
                "var": item["var"],
                sources[0]: str(item[sources[0]]),
                "secret": bool(item.get("secret", False)),
            }
        )
    return tuple(out)


def parse_collection(data: Any, *, max_requests: int = 100) -> Collection:
    """Validate a collection document strictly; raises :class:`CollectionError`."""
    if not isinstance(data, dict):
        raise CollectionError("a collection must be a JSON object")
    if data.get("format") not in (None, FORMAT):
        raise CollectionError(f"unsupported collection format (expected '{FORMAT}')")
    unknown = sorted(set(data) - _COLLECTION_KEYS)
    if unknown:
        # A collection is data. It has no field that could add trust, origins or
        # permissions, and anything unrecognised is refused rather than ignored.
        raise CollectionError(f"unknown collection field(s): {', '.join(unknown[:5])}")
    name = data.get("name")
    if not valid_name(name):
        raise CollectionError("the collection 'name' must be lowercase letters, digits, '-' or '_'")
    raw_requests = data.get("requests")
    if not isinstance(raw_requests, list) or not raw_requests:
        raise CollectionError("a collection needs a non-empty 'requests' list")
    if len(raw_requests) > max_requests:
        raise CollectionError(f"too many requests (limit {max_requests})")
    auth = data.get("auth")
    if auth is not None and not valid_reference(auth):
        raise CollectionError("'auth' must be a credential reference")
    environment = data.get("environment")
    if environment is not None and not isinstance(environment, str):
        raise CollectionError("'environment' must be a name")
    variables = _pairs(data.get("variables"), "variables")
    for key, value in variables:
        if not _VAR_NAME.match(key):
            raise CollectionError(f"'{key[:30]}' is not a valid variable name")
        if "{{" in value:
            raise CollectionError(f"variable '{key}' contains '{{{{…}}}}'; values are literal")
    requests: list[CollectionRequest] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_requests, start=1):
        if not isinstance(raw, dict):
            raise CollectionError(f"request {index} must be an object")
        label = f"request {index}"
        unknown = sorted(set(raw) - _REQUEST_KEYS)
        if unknown:
            raise CollectionError(f"{label}: unknown field(s): {', '.join(unknown[:5])}")
        title = _text(raw.get("name", raw.get("id", label)), f"{label} name", 120)
        request_id = raw.get("id") or slugify(title, f"request-{index}")
        if not isinstance(request_id, str) or not _ID_RE.match(request_id):
            raise CollectionError(f"{label}: 'id' must be lowercase letters, digits, '-' or '_'")
        if request_id in seen:
            raise CollectionError(f"{label}: duplicate id '{request_id}'")
        seen.add(request_id)
        method = str(raw.get("method", "GET")).upper()
        if method not in METHODS:
            raise CollectionError(f"{label}: unsupported method '{method[:12]}'")
        url = _text(raw.get("url"), f"{label} url", 4096)
        if not url.strip():
            raise CollectionError(f"{label}: 'url' is required")
        req_auth = raw.get("auth")
        if req_auth is not None and req_auth != "none" and not valid_reference(req_auth):
            raise CollectionError(f"{label}: 'auth' must be a credential reference or \"none\"")
        timeout = raw.get("timeout")
        if timeout is not None and (
            not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 300
        ):
            raise CollectionError(f"{label}: 'timeout' must be 1-300 seconds")
        try:
            assertions = tuple(parse_assertion(a) for a in (raw.get("assertions") or []))
        except AssertionSyntaxError as exc:
            raise CollectionError(f"{label}: {exc}") from None
        if len(assertions) > 50:
            raise CollectionError(f"{label}: too many assertions")
        try:
            body = _body(raw.get("body"))
            req_headers = _pairs(raw.get("headers"), f"{label} headers", headers=True)
            req_query = _pairs(raw.get("query"), f"{label} query")
            req_extract = _extract(raw.get("extract"))
        except CollectionError as exc:
            raise CollectionError(f"{label}: {exc}") from None
        requests.append(
            CollectionRequest(
                id=request_id,
                name=title,
                method=method,
                url=url,
                headers=req_headers,
                query=req_query,
                body=body,
                auth=req_auth,
                assertions=assertions,
                extract=req_extract,
                timeout=timeout,
                description=_text(raw.get("description", ""), f"{label} description", 500),
            )
        )
    return Collection(
        name=name,
        description=_text(data.get("description", ""), "description", 1000),
        environment=environment,
        auth=auth,
        headers=_pairs(data.get("headers"), "headers", headers=True),
        variables=variables,
        requests=tuple(requests),
    )


def collection_to_dict(collection: Collection) -> dict[str, Any]:
    """The stable JSON form of *collection* (round-trips through :func:`parse_collection`)."""
    out: dict[str, Any] = {"format": FORMAT, "name": collection.name}
    if collection.description:
        out["description"] = collection.description
    if collection.environment:
        out["environment"] = collection.environment
    if collection.auth:
        out["auth"] = collection.auth
    if collection.headers:
        out["headers"] = dict(collection.headers)
    if collection.variables:
        out["variables"] = dict(collection.variables)
    requests = []
    for r in collection.requests:
        entry: dict[str, Any] = {"id": r.id, "name": r.name, "method": r.method, "url": r.url}
        if r.description:
            entry["description"] = r.description
        if r.headers:
            entry["headers"] = dict(r.headers)
        if r.query:
            entry["query"] = dict(r.query)
        if r.body:
            entry["body"] = r.body
        if r.auth:
            entry["auth"] = r.auth
        if r.assertions:
            entry["assertions"] = [a.to_dict() for a in r.assertions]
        if r.extract:
            entry["extract"] = list(r.extract)
        if r.timeout:
            entry["timeout"] = r.timeout
        requests.append(entry)
    out["requests"] = requests
    return out


def fingerprint(collection: Collection, *parts: str) -> str:
    """A stable digest of a collection plus extra context (environment, request id)."""
    canonical = json.dumps(collection_to_dict(collection), sort_keys=True, default=str)
    digest = hashlib.sha256(canonical.encode())
    for part in parts:
        digest.update(b"\x00" + part.encode())
    return digest.hexdigest()


# ── Storage ─────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class CollectionStore:
    """Collections kept as ``<workspace>/<dir>/<name>.json``."""

    root: Path
    max_requests: int = 100

    def path_for(self, name: str) -> Path:
        if not valid_name(name):
            raise CollectionError("a collection name is lowercase letters, digits, '-' or '_'")
        return self.root / f"{name}.json"

    def names(self) -> list[str]:
        try:
            return sorted(p.stem for p in self.root.glob("*.json") if valid_name(p.stem))
        except OSError:
            return []

    def load(self, name: str) -> Collection:
        path = self.path_for(name)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            raise CollectionError(f"no collection named '{name}'") from None
        except OSError:
            raise CollectionError(f"the collection '{name}' could not be read") from None
        if size > MAX_FILE_BYTES:
            raise CollectionError("the collection file is too large")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise CollectionError(f"the collection '{name}' could not be read as JSON") from None
        return parse_collection(data, max_requests=self.max_requests)

    def save(self, collection: Collection, *, overwrite: bool = False) -> Path:
        path = self.path_for(collection.name)
        if path.exists() and not overwrite:
            raise CollectionError(f"a collection named '{collection.name}' already exists")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(collection_to_dict(collection), indent=2) + "\n", encoding="utf-8"
        )
        return path


# ── Postman v2.1 (a scoped subset) ──────────────────────────────────────────


@dataclass(slots=True)
class PostmanImport:
    collection: Collection
    skipped: list[str] = field(default_factory=list)


def _postman_url(url: Any) -> tuple[str, list[tuple[str, str]]]:
    if isinstance(url, str):
        return url, []
    if isinstance(url, dict):
        raw = url.get("raw")
        query = [
            (str(q.get("key", "")), str(q.get("value", "")))
            for q in url.get("query", [])
            if isinstance(q, dict) and not q.get("disabled") and q.get("key")
        ]
        if isinstance(raw, str):
            return raw.split("?", 1)[0], query
        host = ".".join(url.get("host", [])) if isinstance(url.get("host"), list) else ""
        path = "/".join(url.get("path", [])) if isinstance(url.get("path"), list) else ""
        return f"{host}/{path}".strip("/"), query
    return "", []


def _flatten(
    items: Any, prefix: str, out: list[tuple[str, dict[str, Any]]], skipped: list[str]
) -> None:
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        title = f"{prefix}{item.get('name', 'request')}"
        if isinstance(item.get("item"), list):
            _flatten(item["item"], f"{title} / ", out, skipped)
        elif isinstance(item.get("request"), dict):
            out.append((title, item))
            if item.get("event"):
                skipped.append(f"scripts on '{title}' (scripts are never executed)")
            if item["request"].get("auth"):
                skipped.append(
                    f"auth block on '{title}' (configure a credential reference instead)"
                )


def from_postman(data: Any, *, name: str | None = None, max_requests: int = 100) -> PostmanImport:
    """Convert a Postman Collection v2.1 document (supported subset) into a collection."""
    if not isinstance(data, dict) or not isinstance(data.get("item"), list):
        raise CollectionError("this does not look like a Postman Collection v2.1 file")
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    schema = str(info.get("schema", ""))
    if schema and "v2.1" not in schema and "v2.0" not in schema:
        raise CollectionError("only Postman Collection v2.0/v2.1 files can be imported")
    skipped: list[str] = []
    flat: list[tuple[str, dict[str, Any]]] = []
    _flatten(data["item"], "", flat, skipped)
    if data.get("event"):
        skipped.append("collection-level scripts")
    if data.get("auth"):
        skipped.append("collection-level auth block")
    if not flat:
        raise CollectionError("the Postman collection has no requests")
    requests: list[dict[str, Any]] = []
    used: set[str] = set()
    for index, (title, item) in enumerate(flat[:max_requests], start=1):
        req = item["request"]
        url, query = _postman_url(req.get("url"))
        headers = {
            str(h["key"]): str(h.get("value", ""))
            for h in req.get("header", [])
            if isinstance(h, dict) and h.get("key") and not h.get("disabled")
        }
        for key in list(headers):
            if key.lower() in CREDENTIAL_HEADERS and "{{" not in headers[key]:
                skipped.append(f"literal '{key}' header on '{title}' (use a credential reference)")
                del headers[key]
            elif key.lower() in FORBIDDEN_HEADERS:
                del headers[key]
        entry: dict[str, Any] = {
            "id": _unique(slugify(title, f"request-{index}"), used),
            "name": title[:120],
            "method": str(req.get("method", "GET")).upper(),
            "url": url,
        }
        if headers:
            entry["headers"] = headers
        if query:
            entry["query"] = dict(query)
        body = _postman_body(req.get("body"), headers, skipped, title)
        if body:
            entry["body"] = body
        requests.append(entry)
    if len(flat) > max_requests:
        skipped.append(f"{len(flat) - max_requests} request(s) beyond the limit of {max_requests}")
    variables = {
        str(v["key"]): str(v.get("value", ""))
        for v in data.get("variable", [])
        if isinstance(v, dict) and v.get("key") and "{{" not in str(v.get("value", ""))
    }
    document: dict[str, Any] = {
        "format": FORMAT,
        "name": name or slugify(str(info.get("name", "imported")), "imported"),
        "description": str(info.get("description", ""))[:1000]
        if isinstance(info.get("description", ""), str)
        else "",
        "requests": requests,
    }
    if variables:
        document["variables"] = variables
    return PostmanImport(parse_collection(document, max_requests=max_requests), skipped)


def _unique(slug: str, used: set[str]) -> str:
    candidate, counter = slug, 2
    while candidate in used:
        candidate = f"{slug[:44]}-{counter}"
        counter += 1
    used.add(candidate)
    return candidate


def _postman_body(
    body: Any, headers: dict[str, str], skipped: list[str], title: str
) -> dict[str, Any] | None:
    if not isinstance(body, dict) or not body.get("mode") or body.get("mode") == "none":
        return None
    mode = body["mode"]
    if mode == "raw":
        raw = str(body.get("raw", ""))
        language = (body.get("options") or {}).get("raw", {}).get("language", "")
        if language == "json" or headers.get("Content-Type", "").lower().startswith(
            "application/json"
        ):
            try:
                return {"json": json.loads(raw)}
            except ValueError:
                pass  # a templated body is not valid JSON until variables are filled
        return {"text": raw, "content_type": headers.get("Content-Type", "text/plain")}
    if mode in ("urlencoded", "formdata"):
        fields = {}
        for entry in body.get(mode, []):
            if not isinstance(entry, dict) or entry.get("disabled") or not entry.get("key"):
                continue
            if entry.get("type") == "file":
                skipped.append(
                    f"file field '{entry['key']}' on '{title}' (file uploads are not imported)"
                )
                continue
            fields[str(entry["key"])] = str(entry.get("value", ""))
        return {"form" if mode == "urlencoded" else "multipart": fields} if fields else None
    skipped.append(f"'{mode}' body on '{title}'")
    return None


def to_postman(collection: Collection) -> dict[str, Any]:
    """Export as Postman Collection v2.1 (lossy: assertions and extraction are not exported)."""
    items = []
    for r in collection.requests:
        headers = [{"key": k, "value": v} for k, v in (*collection.headers, *r.headers)]
        query = [{"key": k, "value": v} for k, v in r.query]
        raw_url = r.url + ("?" + "&".join(f"{k}={v}" for k, v in r.query) if r.query else "")
        request: dict[str, Any] = {
            "method": r.method,
            "header": headers,
            "url": {"raw": raw_url, "query": query} if query else {"raw": raw_url},
        }
        if r.body:
            if "json" in r.body:
                request["body"] = {
                    "mode": "raw",
                    "raw": json.dumps(r.body["json"], indent=2),
                    "options": {"raw": {"language": "json"}},
                }
            elif "text" in r.body:
                request["body"] = {"mode": "raw", "raw": r.body["text"]}
            else:
                mode = "urlencoded" if "form" in r.body else "formdata"
                fields = r.body.get("form") or r.body.get("multipart") or {}
                request["body"] = {
                    mode: [{"key": k, "value": v} for k, v in fields.items()],
                    "mode": mode,
                }
        items.append({"name": r.name, "request": request})
    return {
        "info": {
            "name": collection.name,
            "description": collection.description,
            "schema": POSTMAN_SCHEMA,
        },
        "item": items,
        "variable": [{"key": k, "value": v} for k, v in collection.variables],
    }
