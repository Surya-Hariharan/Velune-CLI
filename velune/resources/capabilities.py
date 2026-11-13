"""Capability discovery: what each integration can do, whether it can be used now,
and which of its operations the model currently sees.

This module answers *discovery* questions and nothing else. It never executes an
operation and never grants permission: exposing a tool to the model only lets the
model *ask*; every call still runs through ``authorize_and_execute`` and the
permission gate (see :mod:`velune.resources.bridge`). A discovered capability is
not an authorized capability.

Pieces
------
* :class:`IntegrationManifest` / :class:`OperationManifest` — an immutable, validated
  snapshot **derived from the connector** (its ``capabilities()``), so there is one
  source of truth. An invalid connector yields a :class:`ManifestError` and is
  reported, never exposed.
* :func:`model_schema` / :func:`validate_arguments` — the schema the model sees is a
  whitelisted, provider-portable subset; arguments are validated strictly against it
  before anything runs.
* :class:`CapabilityCatalog` — manifests plus availability (installed? reachable?
  configured?) with a short cache, and deterministic task-based suggestions.
* :class:`CapabilityScope` — the per-session set of enabled integrations, with a
  tool budget and a dispatcher overflow so capabilities are never silently dropped.

Nothing here contains or reads a credential. Descriptions are developer-authored,
bounded and passed through redaction as a defence in depth.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from velune.core.redaction import redact_secrets
from velune.permissions.actions import valid_scope
from velune.resources.base import (
    DiscoveryHint,
    ResourceConnector,
    ResourcePermission,
    ResourceState,
)

# ── Naming ──────────────────────────────────────────────────────────────────

#: No ``__`` and no leading/trailing separator, so ``<id>__<action>`` splits unambiguously.
INTEGRATION_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*$")
_ACTION_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_CATEGORY_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
_PARAM_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_KEYWORD_RE = re.compile(r"^[a-z0-9]{2,24}$")
_WORD = re.compile(r"[a-z0-9]+")

MAX_INTEGRATION_ID = 24
MAX_TOOL_NAME = 64
MAX_PARAMS = 16
MAX_ENUM = 32
MAX_DESCRIPTION = 300
MAX_STRING_ARG = 20_000
_SCALARS = frozenset({"string", "integer", "number", "boolean"})
_TYPES = _SCALARS | {"array"}

#: Integration operation tools (plus the dispatcher) shown to the model at once.
DEFAULT_MAX_TOOLS = 24


class ManifestError(ValueError):
    """A connector's declared capabilities are not safe or well-formed to expose."""


def tool_name_for(integration: str, action: str) -> str:
    return f"{integration}__{action}"


# ── Schemas ─────────────────────────────────────────────────────────────────


def _clean_text(value: Any, limit: int = MAX_DESCRIPTION) -> str:
    return redact_secrets(str(value or "")).strip()[:limit]


def model_schema(raw: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Build the model-visible schema for an action's declared parameters.

    Returns ``(schema, constraints)``. The schema is deliberately a *portable
    subset* — flat ``type`` / ``description`` / ``enum`` / ``items`` / ``required``
    only — so it translates unchanged to every provider adapter (Gemini and
    Cohere reject or ignore richer JSON Schema). Range limits are not shown to
    the model; they are kept in *constraints* and enforced by
    :func:`validate_arguments`. Raises :class:`ManifestError` for anything that
    is not a flat object of simple parameters.
    """
    raw = raw or {}
    props = raw.get("properties") or {}
    if not isinstance(props, dict):
        raise ManifestError("parameters must be an object of properties")
    if len(props) > MAX_PARAMS:
        raise ManifestError(f"too many parameters (limit {MAX_PARAMS})")
    properties: dict[str, Any] = {}
    constraints: dict[str, dict[str, Any]] = {}
    for name, spec in props.items():
        if not isinstance(name, str) or not _PARAM_RE.match(name):
            raise ManifestError(f"invalid parameter name {name!r}")
        if not isinstance(spec, dict):
            raise ManifestError(f"parameter '{name}' must be an object")
        kind = spec.get("type")
        if kind not in _TYPES:
            raise ManifestError(f"parameter '{name}' has unsupported type {kind!r}")
        entry: dict[str, Any] = {"type": kind}
        description = _clean_text(spec.get("description"))
        if description:
            entry["description"] = description
        enum = spec.get("enum")
        if enum is not None:
            if kind != "string" or not isinstance(enum, (list, tuple)) or len(enum) > MAX_ENUM:
                raise ManifestError(f"parameter '{name}' has an invalid enum")
            if not all(isinstance(v, str) and v for v in enum):
                raise ManifestError(f"parameter '{name}' enum values must be non-empty strings")
            entry["enum"] = list(enum)
        if kind == "array":
            item_kind = (spec.get("items") or {}).get("type", "string")
            entry["items"] = {"type": item_kind if item_kind in _SCALARS else "string"}
        properties[name] = entry
        limits = {
            key: spec[key]
            for key in ("minimum", "maximum", "maxLength")
            if isinstance(spec.get(key), (int, float)) and not isinstance(spec.get(key), bool)
        }
        if limits:
            constraints[name] = limits
    required = list(raw.get("required") or [])
    if any(r not in properties for r in required):
        raise ManifestError("'required' names a parameter that is not declared")
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema, constraints


def validate_arguments(
    schema: dict[str, Any], constraints: dict[str, dict[str, Any]], arguments: Any
) -> None:
    """Strictly validate model-supplied *arguments*; raise ``ValueError`` if invalid.

    Fail-closed: unknown argument names are rejected, ``bool`` is never a number,
    and ``null`` is never accepted. Messages name the argument and the rule —
    never the offending value, which might be a secret.
    """
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be a JSON object")
    props = schema.get("properties", {})
    unknown = sorted(str(k)[:40] for k in arguments if k not in props)
    if unknown:
        raise ValueError(f"unknown argument(s): {', '.join(unknown[:5])}")
    for name in schema.get("required", []):
        if name not in arguments:
            raise ValueError(f"missing required argument '{name}'")
    for name, value in arguments.items():
        spec = props[name]
        kind = spec["type"]
        ok = (
            (kind == "string" and isinstance(value, str))
            or (kind == "integer" and isinstance(value, int) and not isinstance(value, bool))
            or (
                kind == "number" and isinstance(value, (int, float)) and not isinstance(value, bool)
            )
            or (kind == "boolean" and isinstance(value, bool))
            or (kind == "array" and isinstance(value, list))
        )
        if not ok:
            raise ValueError(f"argument '{name}' must be of type {kind}")
        if "enum" in spec and value not in spec["enum"]:
            raise ValueError(f"argument '{name}' must be one of: {', '.join(spec['enum'])}")
        limits = constraints.get(name, {})
        if kind in ("integer", "number"):
            if "minimum" in limits and value < limits["minimum"]:
                raise ValueError(f"argument '{name}' is below the minimum {limits['minimum']}")
            if "maximum" in limits and value > limits["maximum"]:
                raise ValueError(f"argument '{name}' is above the maximum {limits['maximum']}")
        if kind == "string" and len(value) > min(
            limits.get("maxLength", MAX_STRING_ARG), MAX_STRING_ARG
        ):
            raise ValueError(f"argument '{name}' is too long")
        if kind == "array" and len(value) > 64:
            raise ValueError(f"argument '{name}' has too many items")


# ── Results ─────────────────────────────────────────────────────────────────

#: Cap on a single integration result, in characters of JSON. Smaller than the
#: tool loop's own backstop so that a truncated result is still valid JSON.
MAX_RESULT_CHARS = 12_000


def compact_payload(payload: dict[str, Any], limit: int = MAX_RESULT_CHARS) -> dict[str, Any]:
    """Shrink an oversized result while keeping it valid, structured JSON.

    A list is cut to the items that fit (with the true total); anything else is
    replaced by a bounded preview. The reader is told it was truncated.
    """
    text = json.dumps(payload, default=str, ensure_ascii=False)
    if len(text) <= limit:
        return payload
    out = dict(payload)
    data = payload.get("data")
    budget = max(500, limit - 1500)
    if isinstance(data, dict) and isinstance(data.get("lines"), list):
        # Output of a log-style result: keep the *latest* lines.
        kept: list[Any] = []
        used = 0
        for item in reversed(data["lines"]):
            used += len(json.dumps(item, default=str, ensure_ascii=False)) + 2
            if used > budget:
                break
            kept.append(item)
        kept.reverse()
        out["data"] = {**data, "lines": kept, "truncated": True, "total_lines": len(data["lines"])}
    elif isinstance(data, list):
        items: list[Any] = []
        used = 2
        for item in data:
            used += len(json.dumps(item, default=str, ensure_ascii=False)) + 1
            if used > budget:
                break
            items.append(item)
        out["data"] = {"items": items, "total_items": len(data), "truncated": True}
    else:
        preview = json.dumps(data, default=str, ensure_ascii=False)
        out["data"] = {
            "truncated": True,
            "preview": preview[:budget],
            "original_chars": len(preview),
        }
    warnings = list(out.get("warnings") or [])
    warnings.append(f"Result truncated to fit ({len(text)} characters).")
    out["warnings"] = warnings
    return out


# ── Manifests ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True, eq=False)
class OperationManifest:
    """One operation of an integration, as discovery and the model see it."""

    integration: str
    action: str
    tool_name: str
    description: str
    scope: str
    permission: str  # "read" | "write" | "execute" | "admin"
    destructive: bool
    category: str
    schema: dict[str, Any]
    constraints: dict[str, dict[str, Any]]

    @property
    def read_only(self) -> bool:
        return self.permission == ResourcePermission.READ.value and not self.destructive


@dataclass(frozen=True, slots=True, eq=False)
class IntegrationManifest:
    """A validated, immutable description of one integration's capabilities."""

    id: str
    display_name: str
    description: str
    keywords: tuple[str, ...]
    lifecycle: str
    operations: tuple[OperationManifest, ...]

    @property
    def categories(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for op in self.operations:
            seen.setdefault(op.category, None)
        return tuple(seen)

    def operation(self, action: str) -> OperationManifest | None:
        for op in self.operations:
            if op.action == action:
                return op
        return None

    def fingerprint(self) -> tuple[tuple[str, str, str], ...]:
        """Changes when an operation's name, scope or tier changes."""
        return tuple((o.tool_name, o.scope, o.permission) for o in self.operations)


def build_manifest(connector: ResourceConnector) -> IntegrationManifest:
    """Derive and validate the manifest for *connector* (raises :class:`ManifestError`)."""
    rid = getattr(connector, "resource_id", "") or ""
    if (
        not isinstance(rid, str)
        or len(rid) > MAX_INTEGRATION_ID
        or not INTEGRATION_ID_RE.match(rid)
    ):
        raise ManifestError(f"invalid integration id {rid!r}")
    try:
        capabilities = list(connector.capabilities())
    except Exception as exc:
        raise ManifestError(f"capabilities could not be listed ({type(exc).__name__})") from exc
    if not capabilities:
        raise ManifestError("declares no operations")
    seen: set[str] = set()
    operations: list[OperationManifest] = []
    for cap in capabilities:
        action = cap.action
        if not isinstance(action, str) or not _ACTION_RE.match(action):
            raise ManifestError(f"invalid operation name {action!r}")
        if action in seen:
            raise ManifestError(f"duplicate operation '{action}'")
        seen.add(action)
        tool_name = tool_name_for(rid, action)
        if len(tool_name) > MAX_TOOL_NAME:
            raise ManifestError(f"tool name for '{action}' is too long")
        try:
            scope = connector.scope_for(action, None, cap.permission)
        except Exception as exc:
            raise ManifestError(f"scope for '{action}' could not be determined") from exc
        if not valid_scope(scope):
            raise ManifestError(f"operation '{action}' has an invalid permission scope")
        category = cap.category or (
            "inspect" if cap.permission is ResourcePermission.READ else "change"
        )
        if not _CATEGORY_RE.match(category):
            raise ManifestError(f"operation '{action}' has an invalid category")
        try:
            schema, constraints = model_schema(cap.params)
        except ManifestError as exc:
            raise ManifestError(f"operation '{action}': {exc}") from exc
        operations.append(
            OperationManifest(
                integration=rid,
                action=action,
                tool_name=tool_name,
                description=_clean_text(cap.description or action),
                scope=scope,
                permission=cap.permission.value,
                destructive=bool(cap.destructive),
                category=category,
                schema=schema,
                constraints=constraints,
            )
        )
    keywords = tuple(
        k
        for k in (str(w).lower() for w in (getattr(connector, "keywords", ()) or ()))
        if _KEYWORD_RE.match(k)
    )
    return IntegrationManifest(
        id=rid,
        display_name=_clean_text(connector.display_name or rid, 60),
        description=_clean_text(getattr(connector, "description", ""), 240),
        keywords=keywords,
        lifecycle=str(getattr(connector, "lifecycle", "stateless") or "stateless"),
        operations=tuple(operations),
    )


# ── Availability + catalog ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Availability:
    """Whether an integration can be used *right now*. Never carries secrets."""

    state: str  # connected | available | unavailable | needs_configuration | invalid | unknown
    reason: str = ""
    checked_at: float = 0.0

    @property
    def usable(self) -> bool:
        return self.state in ("connected", "available")


class CapabilityCatalog:
    """Manifests and availability for every integration a manager offers.

    Manifests are cached per connector instance. Availability is probed lazily
    with a short TTL (a probe can run a subprocess or open a socket, so it is
    never done per model turn). A connection that is currently open is always
    reported from live status, and :meth:`note_failure` lets a failed call mark an
    integration unavailable immediately instead of waiting out the TTL. Nothing
    cached here is an authorization decision.
    """

    def __init__(
        self,
        manager: Any,
        *,
        availability_ttl: float = 30.0,
        probe_timeout: float = 6.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._manager = manager
        self._ttl = availability_ttl
        self._probe_timeout = probe_timeout
        self._clock = clock
        self._manifests: dict[str, tuple[int, IntegrationManifest | ManifestError]] = {}
        self._availability: dict[str, Availability] = {}
        #: Calls that just failed because the application is gone. Outranks live
        #: connection status (a socket can look open while the app is dead) until
        #: the TTL passes, a call succeeds, or availability is re-probed.
        self._failures: dict[str, Availability] = {}

    # -- manifests --

    def _entry(self, integration_id: str) -> IntegrationManifest | ManifestError | None:
        connector = self._manager.get(integration_id)
        if connector is None:
            self._manifests.pop(integration_id, None)
            return None
        cached = self._manifests.get(integration_id)
        if cached is not None and cached[0] == id(connector):
            return cached[1]
        try:
            built: IntegrationManifest | ManifestError = build_manifest(connector)
        except ManifestError as exc:
            built = exc
        self._manifests[integration_id] = (id(connector), built)
        return built

    def ids(self) -> list[str]:
        """Enabled, registered integration ids in registration order."""
        return list(self._manager.list_ids())

    def manifest(self, integration_id: str) -> IntegrationManifest | None:
        entry = self._entry(integration_id)
        return entry if isinstance(entry, IntegrationManifest) else None

    def problem(self, integration_id: str) -> str | None:
        entry = self._entry(integration_id)
        return str(entry) if isinstance(entry, ManifestError) else None

    def manifests(self) -> list[IntegrationManifest]:
        """Valid manifests, in registration order.

        Tool names cannot collide between integrations: an id never contains
        ``__`` (see ``INTEGRATION_ID_RE``) and every name is ``<id>__<action>``,
        so the split is unambiguous.
        """
        return [m for iid in self.ids() if (m := self.manifest(iid)) is not None]

    def problems(self) -> dict[str, str]:
        """Integrations whose declared capabilities were rejected, with the reason."""
        return {iid: p for iid in self.ids() if (p := self.problem(iid))}

    # -- availability --

    def invalidate(self, integration_id: str | None = None) -> None:
        if integration_id is None:
            self._availability.clear()
            self._failures.clear()
        else:
            self._availability.pop(integration_id, None)
            self._failures.pop(integration_id, None)

    def note_failure(self, integration_id: str, reason: str) -> None:
        """A call just failed because the application is gone: reflect it now."""
        self._failures[integration_id] = Availability(
            "unavailable", _clean_text(reason, 200), self._clock()
        )

    def note_success(self, integration_id: str) -> None:
        """A call just worked: the application is evidently there."""
        self._failures.pop(integration_id, None)

    async def availability(self, integration_id: str, *, refresh: bool = False) -> Availability:
        connector = self._manager.get(integration_id)
        if connector is None:
            return Availability("unknown", "unknown or disabled integration")
        problem = self.problem(integration_id)
        if problem:
            return Availability("invalid", _clean_text(problem, 200))
        now = self._clock()
        failure = self._failures.get(integration_id)
        if failure is not None:
            if not refresh and now - failure.checked_at < self._ttl:
                return failure
            self._failures.pop(integration_id, None)
        try:
            live = connector.status().state
        except Exception:
            live = ResourceState.ERROR
        if live is ResourceState.CONNECTED:
            return Availability("connected", "", now)
        cached = self._availability.get(integration_id)
        if cached is not None and not refresh and now - cached.checked_at < self._ttl:
            return cached
        result = await self._probe(connector, now)
        self._availability[integration_id] = result
        return result

    async def _probe(self, connector: ResourceConnector, now: float) -> Availability:
        try:
            problem = connector.configuration_problem()
        except Exception:
            problem = "its configuration could not be checked"
        if problem:
            return Availability("needs_configuration", _clean_text(problem, 200), now)
        try:
            ok, reason = await asyncio.wait_for(
                connector.probe_availability(), timeout=self._probe_timeout
            )
        except asyncio.TimeoutError:
            ok, reason = False, "did not respond in time"
        except asyncio.CancelledError:
            raise
        except Exception:
            ok, reason = False, "could not be probed"
        return Availability("available" if ok else "unavailable", _clean_text(reason, 200), now)

    # -- selection --

    def suggest(self, task_text: str, hints: Sequence[DiscoveryHint] = ()) -> list[str]:
        """Integration ids relevant to *task_text* — deterministic, no model involved.

        A suggestion only *highlights* an integration in discovery output; it
        never exposes tools or grants anything. Score = whole-word keyword hits,
        +2 when the integration's own id is a word of the task, +2 per workspace
        discovery hint (a compose file suggests ``docker``). Ties keep
        registration order.
        """
        words = set(_WORD.findall((task_text or "").lower()))
        hinted = {h.resource_id for h in hints}
        scored: list[tuple[int, int, str]] = []
        for index, m in enumerate(self.manifests()):
            score = sum(1 for k in m.keywords if k in words)
            score += 2 if m.id in words else 0
            score += 2 if m.id in hinted else 0
            if score > 0:
                scored.append((-score, index, m.id))
        return [iid for _, _, iid in sorted(scored)]

    async def describe(
        self,
        *,
        enabled: Sequence[str] = (),
        query: str | None = None,
        task_text: str = "",
        hints: Sequence[DiscoveryHint] = (),
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        """JSON-safe discovery rows for the model. Contains no credentials or paths."""
        suggested = set(self.suggest(task_text, hints))
        needle = (query or "").strip().lower()
        rows: list[dict[str, Any]] = []
        manifests = {m.id: m for m in self.manifests()}
        problems = self.problems()
        ids = [i for i in self.ids() if i in manifests or i in problems]
        avail = await asyncio.gather(*(self.availability(i, refresh=refresh) for i in ids))
        for iid, state in zip(ids, avail, strict=True):
            m = manifests.get(iid)
            if m is None:
                rows.append(
                    {"id": iid, "state": "invalid", "reason": state.reason, "enabled": False}
                )
                continue
            if needle:
                hay = " ".join(
                    [m.id, m.display_name, m.description, *m.keywords, *m.categories]
                    + [o.action for o in m.operations]
                ).lower()
                if needle not in hay:
                    continue
            row: dict[str, Any] = {
                "id": m.id,
                "name": m.display_name,
                "description": m.description,
                "state": state.state,
                "enabled": iid in enabled,
                "suggested": iid in suggested,
                "operations": len(m.operations),
                "categories": [
                    {
                        "name": c,
                        "operations": [o.action for o in m.operations if o.category == c],
                    }
                    for c in m.categories
                ],
            }
            if state.reason:
                row["reason"] = state.reason
            rows.append(row)
        return rows


# ── Scope ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True, eq=False)
class EnableResult:
    exposed: tuple[OperationManifest, ...]
    dispatched: tuple[OperationManifest, ...]
    changed: bool
    unknown_categories: tuple[str, ...] = ()


class CapabilityScope:
    """The integrations the model may currently *ask* to use, with a tool budget.

    Up to ``max_tools`` operations (counting the dispatcher) are exposed as their
    own tools. When more are enabled, the overflow is reachable through a single
    ``integration_call`` dispatcher tool — a controlled fallback, so enabling an
    integration never silently drops one of its operations. Order is the order of
    enabling, so earlier tools keep their position (a stable tool list keeps
    provider prompt caches valid for as long as possible).

    ``version`` changes exactly when the exposed or dispatch set changes.
    """

    def __init__(self, max_tools: int = DEFAULT_MAX_TOOLS) -> None:
        self.max_tools = max(4, int(max_tools))
        self._enabled: dict[str, tuple[IntegrationManifest, frozenset[str]]] = {}
        self._exposed: tuple[OperationManifest, ...] = ()
        self._dispatch: tuple[OperationManifest, ...] = ()
        self._version = 0

    @property
    def version(self) -> int:
        return self._version

    @property
    def exposed(self) -> tuple[OperationManifest, ...]:
        return self._exposed

    @property
    def dispatched(self) -> tuple[OperationManifest, ...]:
        return self._dispatch

    def enabled_ids(self) -> list[str]:
        return list(self._enabled)

    def operation(self, integration_id: str, action: str) -> OperationManifest | None:
        """The operation if — and only if — it is currently enabled."""
        for op in (*self._exposed, *self._dispatch):
            if op.integration == integration_id and op.action == action:
                return op
        return None

    def is_enabled(self, integration_id: str, action: str) -> bool:
        return self.operation(integration_id, action) is not None

    def enable(
        self, manifest: IntegrationManifest, categories: Sequence[str] | None = None
    ) -> EnableResult:
        """Enable *manifest* (idempotent). Unknown categories are reported, not fatal."""
        wanted = {c for c in (categories or ()) if isinstance(c, str)}
        known = set(manifest.categories)
        unknown = tuple(sorted(wanted - known))
        chosen = frozenset(wanted & known) if wanted else frozenset(known)
        previous = self._enabled.get(manifest.id)
        merged = chosen | (previous[1] if previous else frozenset())
        if chosen:
            self._enabled[manifest.id] = (manifest, merged)
        changed = self._recompute()
        mine_exposed = tuple(o for o in self._exposed if o.integration == manifest.id)
        mine_dispatch = tuple(o for o in self._dispatch if o.integration == manifest.id)
        return EnableResult(mine_exposed, mine_dispatch, changed, unknown)

    def disable(self, integration_id: str) -> bool:
        if integration_id not in self._enabled:
            return False
        del self._enabled[integration_id]
        self._recompute()
        return True

    def reset(self) -> None:
        self._enabled.clear()
        self._recompute()

    def refresh(self, catalog: CapabilityCatalog) -> bool:
        """Re-sync with the catalog; drop what no longer exists, pick up changes.

        Called at the start of a run so a connector that was removed, disabled or
        redefined since the last turn cannot leave a stale tool behind.
        """
        changed = False
        for iid, (old, cats) in list(self._enabled.items()):
            current = catalog.manifest(iid)
            if current is None:
                del self._enabled[iid]
                changed = True
            elif current.fingerprint() != old.fingerprint():
                self._enabled[iid] = (current, cats & frozenset(current.categories))
                changed = True
        return self._recompute() or changed

    def _recompute(self) -> bool:
        ordered = [
            op for m, cats in self._enabled.values() for op in m.operations if op.category in cats
        ]
        reserve = 1 if len(ordered) > self.max_tools else 0
        limit = self.max_tools - reserve
        exposed, dispatch = tuple(ordered[:limit]), tuple(ordered[limit:])
        same = [o.tool_name for o in exposed] == [o.tool_name for o in self._exposed] and [
            o.tool_name for o in dispatch
        ] == [o.tool_name for o in self._dispatch]
        self._exposed, self._dispatch = exposed, dispatch
        if not same:
            self._version += 1
        return not same
