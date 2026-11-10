"""The ``http`` connector: REST APIs and local dev servers, through the resource runtime.

One connector, declared through the same manifest mechanism as Docker or Postgres,
so the A2 discovery tools (``integrations_list`` / ``integrations_enable``), the
tool budget and the ``integration_call`` dispatcher expose it with no special
cases. Every call is described *before* it runs (:meth:`describe_actions`) and
decided by the permission gate; nothing here authorizes anything.

Operations (categories in brackets):

``request``, ``diagnose``                     [requests]  send one request / find why it fails
``environments``                              [environments]
``collection_list`` / ``collection_show`` / ``collection_run`` / ``collection_import`` /
``collection_export``                         [collections]
``openapi_load`` / ``openapi_search`` / ``openapi_call``   [openapi]

How a request is judged (see :class:`Plan`): the HTTP method sets the base tier
(GET/HEAD/OPTIONS read, POST/PUT/PATCH write, DELETE delete) and is then
*escalated* by what the call touches — a ``GET /users/1/delete`` is treated as a
write; a write to a production environment, to a sensitive local service port, to
an untrusted remote origin or with a file upload is high-risk (confirmed in every
mode, never covered by a session grant); a destination that is not a trusted
origin adds a network action; a destination the policy forbids is blocked in every
mode. The judgement uses the URL text only — the connection-time check in
:mod:`~velune.resources.http.transport` is what enforces the destination.

A **collection run** is authorized against *every* request in the collection at
once, and the run refuses to start if the collection changed after it was
authorized, so a collection cannot be a way around approval.
"""

from __future__ import annotations

import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from velune.core.redaction import register_secret_value
from velune.resources.base import (
    DiscoveryHint,
    ErrorCode,
    Lifecycle,
    ResourceCapability,
    ResourceConnector,
    ResourcePermission,
    ResourceResult,
    ResourceState,
    ResourceStatus,
)
from velune.resources.http import openapi as oa
from velune.resources.http.assertions import (
    AssertionSyntaxError,
    evaluate,
    is_missing,
    json_lookup,
    parse_assertions,
)
from velune.resources.http.auth import (
    CredentialError,
    TokenCache,
    apply_credential,
    load_credential,
    oauth2_token,
    valid_reference,
)
from velune.resources.http.client import (
    METHODS,
    HttpExecutor,
    HttpFailure,
    HttpLimits,
    HttpOutcome,
    OutRequest,
)
from velune.resources.http.collections import (
    Collection,
    CollectionError,
    CollectionRequest,
    CollectionStore,
    collection_to_dict,
    fingerprint,
    from_postman,
    parse_collection,
    to_postman,
    valid_name,
)
from velune.resources.http.environments import (
    Environment,
    VariableError,
    VariableScope,
)
from velune.resources.http.netpolicy import (
    Destination,
    Kind,
    NetworkPolicy,
    ParsedUrl,
    PolicyViolation,
    parse_url,
)
from velune.resources.http.present import (
    clean_headers,
    present_body,
    redact_url,
    redirect_entries,
    select_paths,
    status_outcome,
    view_of,
)

MAX_BODY_JSON_CHARS = 1_000_000
MAX_DEADLINE = 120
MAX_COLLECTION_ACTIONS = 25
MAX_SPECS = 8
_STATE_WORDS = frozenset(
    "logout signout delete remove destroy drop reset revoke purge wipe terminate shutdown "
    "restart kill cancel deactivate disable unsubscribe truncate clear flush".split()
)
_WORD = re.compile(r"[a-z0-9]+")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_PLACEHOLDER = "placeholder-secret-value"

_METHOD_PARAM = {
    "type": "string",
    "description": "HTTP method.",
    "enum": list(METHODS),
}
_ENV = {
    "type": "string",
    "description": "Environment name (see the environments operation). Optional when one is the default.",
}
_AUTH = {
    "type": "string",
    "description": "Credential reference to authenticate with (never a secret itself). "
    "Defaults to the environment's credential; use 'none' to send no credentials.",
}
_EXPECT = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Assertions checked locally, e.g. 'status == 200', 'status in 200-299', "
    "'header content-type contains json', 'json data.id exists', 'json data.status == active', "
    "'json data.items type array', 'body contains ok', 'duration < 500'.",
}
_SELECT = {
    "type": "array",
    "items": {"type": "string"},
    "description": "JSON paths (data.items[0].id) to return from a large JSON response.",
}
_BODY_PARAMS = {
    "json": {"type": "string", "description": "JSON request body, as a JSON document string."},
    "form": {
        "type": "array",
        "items": {"type": "string"},
        "description": "URL-encoded form fields as 'name=value'.",
    },
    "multipart": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Multipart fields: 'name=value', or 'name=@relative/path' to upload a workspace file.",
    },
    "body": {"type": "string", "description": "Raw request body (needs content_type)."},
    "content_type": {"type": "string", "description": "Content-Type for a raw body."},
}
_COMMON_REQUEST = {
    "query": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Query parameters as 'name=value'.",
    },
    "headers": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Extra headers as 'Name: value'. Credentials belong in 'auth', not here.",
    },
    **_BODY_PARAMS,
    "auth": _AUTH,
    "timeout": {
        "type": "integer",
        "description": "Overall time limit in seconds (default from settings, at most 120).",
        "minimum": 1,
        "maximum": MAX_DEADLINE,
    },
    "expect": _EXPECT,
    "select": _SELECT,
}


# ── Normalized request input ────────────────────────────────────────────────


@dataclass(slots=True)
class RequestInput:
    """One request, from any source (an ad-hoc call, an OpenAPI call, a collection)."""

    method: str
    url: str
    query: list[tuple[str, str]] = field(default_factory=list)
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: dict[str, Any] | None = None
    auth: str | None = None
    environment: str | None = None
    timeout: int | None = None
    assertions: list[Any] = field(default_factory=list)
    select: list[str] = field(default_factory=list)
    follow_redirects: bool = True
    variables: dict[str, str] = field(default_factory=dict)
    secret_variables: frozenset[str] = frozenset()
    extract: tuple[dict[str, Any], ...] = ()


@dataclass(slots=True)
class Plan:
    """What a request would do, judged from its text alone (no I/O, no secrets)."""

    method: str
    permission: ResourcePermission = ResourcePermission.READ
    destructive: bool = False
    high_risk: bool = False
    problems: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    url: ParsedUrl | None = None
    destination: Destination | None = None
    environment: Environment | None = None
    uploads: list[str] = field(default_factory=list)

    @property
    def preview(self) -> str:
        if self.url is None:
            return f"{self.method} (target could not be determined)"
        text = f"{self.method} {redact_url(str(self.url))}"
        if self.environment is not None:
            text += f" [env {self.environment.name}]"
        if self.uploads:
            text += f" (uploads {', '.join(self.uploads[:3])})"
        return text[:300]


_RANK = {
    ResourcePermission.READ: 0,
    ResourcePermission.WRITE: 1,
    ResourcePermission.EXECUTE: 1,
    ResourcePermission.ADMIN: 2,
}


class HttpConnector(ResourceConnector):
    """Send HTTP requests, run collections with assertions, explore OpenAPI specs."""

    resource_id = "http"
    display_name = "HTTP APIs"
    lifecycle = Lifecycle.STATELESS
    description = (
        "Send HTTP requests to REST APIs and local dev servers, check responses with assertions, "
        "run API collections, and explore OpenAPI specifications."
    )
    keywords = (
        "http",
        "https",
        "api",
        "apis",
        "endpoint",
        "endpoints",
        "curl",
        "postman",
        "openapi",
        "swagger",
        "localhost",
    )
    #: A failed request to the *target* says nothing about this integration being
    #: usable, so a connection error must not mark it unavailable.
    failures_mark_unavailable = False

    def __init__(
        self,
        name: str | None = None,
        *,
        config: Any = None,
        workspace: Path | str | None = None,
        workspace_trusted: bool | None = None,
        executor: HttpExecutor | None = None,
    ) -> None:
        super().__init__(name)
        from velune.kernel.config import HttpConfig

        self._problem: str | None = None
        try:
            if config is None:
                self.config = HttpConfig()
            elif isinstance(config, HttpConfig):
                self.config = config
            else:
                self.config = HttpConfig.model_validate(config)
        except Exception:
            self.config = HttpConfig()
            self._problem = "the [resources.http] settings are invalid; defaults are in use"
        self.workspace = Path(workspace).resolve() if workspace else None
        if workspace_trusted is None:
            workspace_trusted = True
            if self.workspace is not None:
                try:
                    from velune.core.trust import is_trusted

                    workspace_trusted = bool(is_trusted(self.workspace))
                except Exception:
                    workspace_trusted = False
        self.workspace_trusted = bool(workspace_trusted)
        self.environments: dict[str, Environment] = {}
        self.environment_problems: dict[str, str] = {}
        for env_name, env_config in self.config.environments.items():
            try:
                self.environments[env_name] = Environment.from_config(env_name, env_config)
            except VariableError as exc:
                self.environment_problems[env_name] = str(exc)
        self.policy = self._build_policy()
        self.limits = HttpLimits(
            connect_timeout=self.config.connect_timeout,
            read_timeout=self.config.read_timeout,
            write_timeout=self.config.write_timeout,
            deadline=self.config.timeout,
            max_response_bytes=self.config.max_response_bytes,
            max_request_bytes=self.config.max_request_bytes,
            max_redirects=self.config.max_redirects,
            max_connections=self.config.max_connections,
        )
        self.executor = executor or HttpExecutor(self.policy, self.limits)
        self._tokens = TokenCache()
        self._redact_fields = frozenset(f.lower() for f in self.config.redact_response_fields)
        self._specs: OrderedDict[str, oa.ApiCatalog] = OrderedDict()
        self._authorized: OrderedDict[str, float] = OrderedDict()
        self._store: CollectionStore | None = None
        if self.workspace is not None:
            root = (self.workspace / self.config.collections_dir).resolve()
            try:
                root.relative_to(self.workspace)
                self._store = CollectionStore(root, self.config.max_collection_requests)
            except ValueError:
                self._problem = "collections_dir must be inside the workspace"

    # ── Policy ───────────────────────────────────────────────────────────────

    def _build_policy(self) -> NetworkPolicy:
        """Trust only what the user vouched for — and only in a trusted workspace."""
        if not self.workspace_trusted:
            return NetworkPolicy(allow_loopback=self.config.allow_loopback)
        origins = list(self.config.trusted_origins)
        origins += [str(o) for env in self.environments.values() for o in env.origins]
        return NetworkPolicy.from_origins(
            origins,
            allow_private_network=self.config.allow_private_network,
            allow_loopback=self.config.allow_loopback,
        )

    # ── Lifecycle: stateless ─────────────────────────────────────────────────

    async def connect(self) -> ResourceResult:
        return ResourceResult.success("connect")

    async def disconnect(self) -> ResourceResult:
        self._tokens.clear()
        return ResourceResult.success("disconnect")

    def status(self) -> ResourceStatus:
        count = len(self._store.names()) if self._store else 0
        detail = f"{len(self.environments)} environment(s), {count} collection(s)"
        if not self.workspace_trusted and (self.config.trusted_origins or self.environments):
            detail += "; configured origins ignored (workspace not trusted)"
        return ResourceStatus(
            self.resource_id, self.display_name, ResourceState.CONNECTED, detail=detail
        )

    async def discover(self) -> list[DiscoveryHint]:
        return []

    async def probe_availability(self) -> tuple[bool, str]:
        return True, ""

    def configuration_problem(self) -> str | None:
        return None  # works with no configuration at all

    # ── Capabilities ─────────────────────────────────────────────────────────

    def capabilities(self) -> list[ResourceCapability]:
        read, write = ResourcePermission.READ, ResourcePermission.WRITE
        method = {"method": _METHOD_PARAM}
        url = {
            "url": {
                "type": "string",
                "description": "Absolute URL, or a path starting with '/' (joined to the environment's "
                "base URL). May contain {{variables}}.",
            }
        }
        name = {"type": "string", "description": "Name."}
        return [
            ResourceCapability(
                "request",
                read,
                "Send an HTTP request (GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS) and inspect the "
                "structured response; optionally check it with assertions",
                scope="http.request.read",
                category="requests",
                params={
                    "properties": {**method, **url, "environment": _ENV, **_COMMON_REQUEST},
                    "required": ["method", "url"],
                },
            ),
            ResourceCapability(
                "diagnose",
                read,
                "Find why an API is not responding: check the URL, DNS, connection, TLS and a "
                "lightweight request, and report which step fails",
                scope="http.request.read",
                category="requests",
                params={
                    "properties": {
                        **url,
                        "environment": _ENV,
                        "method": {
                            "type": "string",
                            "description": "HEAD (default) or GET.",
                            "enum": ["HEAD", "GET"],
                        },
                    },
                    "required": ["url"],
                },
            ),
            ResourceCapability(
                "environments",
                read,
                "List the configured API environments (base URL, variable names, never secret values)",
                scope="http.environment.read",
                category="environments",
                params={
                    "properties": {"name": {"type": "string", "description": "One environment."}}
                },
            ),
            ResourceCapability(
                "collection_list",
                read,
                "List the saved API collections",
                scope="http.collection.read",
                category="collections",
            ),
            ResourceCapability(
                "collection_show",
                read,
                "Show a collection's requests, assertions and settings",
                scope="http.collection.read",
                category="collections",
                params={"properties": {"name": name}, "required": ["name"]},
            ),
            ResourceCapability(
                "collection_run",
                read,
                "Run a collection (or one request of it) in order and summarize passes and failures. "
                "Every request is authorized together before anything is sent",
                scope="http.collection.run",
                category="collections",
                params={
                    "properties": {
                        "name": name,
                        "request": {"type": "string", "description": "Run only this request id."},
                        "environment": _ENV,
                        "stop_on_failure": {
                            "type": "boolean",
                            "description": "Stop at the first failed request.",
                        },
                    },
                    "required": ["name"],
                },
            ),
            ResourceCapability(
                "collection_import",
                write,
                "Import a collection from a workspace file (Velune format, or a Postman v2.1 subset)",
                scope="http.collection.write",
                category="collections",
                params={
                    "properties": {
                        "file": {
                            "type": "string",
                            "description": "Path of the file in the workspace.",
                        },
                        "format": {
                            "type": "string",
                            "description": "File format (default velune).",
                            "enum": ["velune", "postman"],
                        },
                        "name": {"type": "string", "description": "Name to save it under."},
                        "overwrite": {
                            "type": "boolean",
                            "description": "Replace an existing collection.",
                        },
                    },
                    "required": ["file"],
                },
            ),
            ResourceCapability(
                "collection_export",
                write,
                "Export a collection to a file in the workspace (Velune format, or Postman v2.1)",
                scope="http.collection.write",
                category="collections",
                params={
                    "properties": {
                        "name": name,
                        "file": {
                            "type": "string",
                            "description": "Destination .json path in the workspace.",
                        },
                        "format": {
                            "type": "string",
                            "description": "File format (default velune).",
                            "enum": ["velune", "postman"],
                        },
                        "overwrite": {
                            "type": "boolean",
                            "description": "Replace an existing file.",
                        },
                    },
                    "required": ["name", "file"],
                },
            ),
            ResourceCapability(
                "openapi_load",
                read,
                "Load an OpenAPI 3.x specification from a workspace file or a URL so its endpoints can "
                "be searched and called",
                scope="http.openapi.read",
                category="openapi",
                params={
                    "properties": {
                        "file": {
                            "type": "string",
                            "description": "Specification file in the workspace.",
                        },
                        "url": {"type": "string", "description": "Specification URL."},
                        "name": {"type": "string", "description": "Name to refer to it by."},
                        "environment": _ENV,
                    }
                },
            ),
            ResourceCapability(
                "openapi_search",
                read,
                "Search a loaded OpenAPI specification for endpoints, or show one endpoint's parameters, "
                "request body and responses",
                scope="http.openapi.read",
                category="openapi",
                params={
                    "properties": {
                        "spec": {"type": "string", "description": "Loaded specification name."},
                        "query": {"type": "string", "description": "Words to look for."},
                        "operation": {
                            "type": "string",
                            "description": "Show this operation in detail.",
                        },
                        "tag": {"type": "string", "description": "Only this tag."},
                        "method": {
                            "type": "string",
                            "description": "Only this method.",
                            "enum": list(METHODS),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Most results (default 15).",
                            "minimum": 1,
                            "maximum": 50,
                        },
                    },
                    "required": ["spec"],
                },
            ),
            ResourceCapability(
                "openapi_call",
                read,
                "Call an endpoint of a loaded OpenAPI specification; the request is checked against the "
                "specification and then judged like any other request",
                scope="http.request.read",
                category="openapi",
                params={
                    "properties": {
                        "spec": {"type": "string", "description": "Loaded specification name."},
                        "operation": {
                            "type": "string",
                            "description": "Operation id from the search.",
                        },
                        "path_params": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Path parameters as 'name=value'.",
                        },
                        "environment": _ENV,
                        **_COMMON_REQUEST,
                    },
                    "required": ["spec", "operation"],
                },
            ),
        ]

    # ── Scope / tier ─────────────────────────────────────────────────────────

    _REQUEST_SCOPES = {
        ResourcePermission.READ: "http.request.read",
        ResourcePermission.WRITE: "http.request.write",
        ResourcePermission.EXECUTE: "http.request.write",
        ResourcePermission.ADMIN: "http.request.delete",
    }

    def scope_for(
        self, action: str, params: dict[str, Any] | None, permission: ResourcePermission
    ) -> str:
        if action in ("request", "diagnose", "openapi_call", "collection_run"):
            if action == "collection_run" and permission is ResourcePermission.READ:
                return "http.collection.run"
            return self._REQUEST_SCOPES[permission]
        cap = self.capability_for(action)
        return cap.scope if cap and cap.scope else f"http.{action}"

    def effective_permission(
        self, action: str, params: dict[str, Any] | None
    ) -> tuple[ResourcePermission, bool] | None:
        if action in ("request", "diagnose", "openapi_call"):
            plan = self._plan_for(action, params or {})
            if plan is not None:
                return plan.permission, plan.destructive
        elif action == "collection_run":
            plans = self._collection_plans(params or {})
            if plans:
                top = max(plans, key=lambda p: (p.destructive, _RANK[p.permission]))
                return top.permission, top.destructive
        return None

    def authorization_preview(self, action: str, params: dict[str, Any] | None) -> str:
        params = params or {}
        if action in ("request", "diagnose", "openapi_call"):
            plan = self._plan_for(action, params)
            return plan.preview if plan else f"http {action}"
        if action == "collection_run":
            plans = self._collection_plans(params)
            lines = [p.preview for p in plans[:8]]
            if len(plans) > 8:
                lines.append(f"… and {len(plans) - 8} more request(s)")
            return "\n".join(lines) or f"run collection {params.get('name', '')}"
        if action in ("collection_import", "collection_export"):
            return f"{action} {params.get('file', '')}"
        return f"http {action}"

    # ── Describing a call to the permission gate ─────────────────────────────

    def describe_actions(
        self, action: str, params: dict[str, Any] | None, *, ledger: Any = None
    ) -> list[Any]:
        from velune.permissions.actions import Action, ActionType, Risk

        params = params or {}
        if action in ("request", "diagnose", "openapi_call"):
            plan = self._plan_for(action, params)
            return (
                self._actions_for(plan)
                if plan is not None
                else super().describe_actions(action, params, ledger=ledger)
            )
        if action == "openapi_load":
            if params.get("url"):
                plan = self._plan_input(
                    RequestInput("GET", str(params["url"]), environment=params.get("environment"))
                )
                actions = self._actions_for(plan)
                # Loading a spec is a read of the spec, whatever scope the GET carried.
                return [replace(a, scope="http.openapi.read") if a.scope else a for a in actions]
            return super().describe_actions(action, params, ledger=ledger)
        if action == "collection_run":
            plans = self._collection_plans(params)
            if not plans:
                return super().describe_actions(action, params, ledger=ledger)
            actions: list[Any] = []
            seen: set[tuple[Any, ...]] = set()
            for plan in plans:
                for item in self._actions_for(plan):
                    key = (item.action_type, item.scope, item.target, item.risk, item.blocked)
                    if key not in seen:
                        seen.add(key)
                        actions.append(item)
            if len(actions) > MAX_COLLECTION_ACTIONS:
                # Too many distinct destinations to review one by one: ask once, loudly.
                actions = actions[: MAX_COLLECTION_ACTIONS - 1] + [
                    Action(
                        ActionType.INTEGRATION_EXECUTE,
                        f"http:{params.get('name', '')}",
                        f"collection with {len(plans)} requests to many destinations",
                        Risk.HIGH,
                        scope="http.collection.run",
                    )
                ]
            self._remember_authorization(params)
            return actions
        return super().describe_actions(action, params, ledger=ledger)

    def _actions_for(self, plan: Plan) -> list[Any]:
        from velune.permissions.actions import Action, ActionType, Risk

        perm = plan.permission
        scope = self._REQUEST_SCOPES[perm]
        if plan.destructive:
            kind, risk = ActionType.INTEGRATION_DELETE, Risk.HIGH
        elif perm is ResourcePermission.READ:
            kind, risk = ActionType.INTEGRATION_READ, Risk.LOW
        else:
            kind, risk = ActionType.INTEGRATION_WRITE, Risk.MEDIUM
        if plan.high_risk:
            risk = Risk.HIGH
        target = f"{plan.method} {redact_url(str(plan.url))}" if plan.url else f"http:{plan.method}"
        blocked = plan.destination.blocked if plan.destination else ""
        reason = "; ".join(plan.reasons) or plan.preview
        actions = [
            Action(
                kind,
                target[:300],
                reason[:300],
                risk,
                blocked=bool(blocked),
                detail=(blocked or plan.preview)[:300],
                scope=scope,
            )
        ]
        dest = plan.destination
        if dest is not None and not blocked and not dest.trusted:
            # A destination nobody vouched for: sending data there is a network action
            # of its own (confirmed in MANUAL; sensitive local ports always confirmed).
            net_risk = Risk.HIGH if dest.sensitive or dest.kind is Kind.PRIVATE else Risk.MEDIUM
            actions.append(
                Action(
                    ActionType.NETWORK,
                    f"{plan.url.origin}",
                    "; ".join(dest.notes)
                    or f"{dest.kind.value} destination that is not a trusted origin",
                    net_risk,
                )
            )
        return actions

    # ── Planning (pure: no I/O, no keystore, no DNS) ────────────────────────

    def _plan_for(self, action: str, params: dict[str, Any]) -> Plan | None:
        """The plan for a request-shaped action, or None when it cannot be built."""
        try:
            if action == "openapi_call":
                inp = self._openapi_input(params, validate=False)
            else:
                inp = self._request_input(
                    params,
                    method=("HEAD" if action == "diagnose" and not params.get("method") else None),
                )
        except HttpFailure:
            return None
        return self._plan_input(inp)

    def _plan_input(self, inp: RequestInput) -> Plan:
        plan = Plan(method=inp.method)
        # The method alone sets the base tier, so even a request whose target cannot
        # be worked out is gated as what it would be — never as a read by default.
        method = inp.method
        plan.permission, plan.destructive = {
            "DELETE": (ResourcePermission.ADMIN, True),
            "POST": (ResourcePermission.WRITE, False),
            "PUT": (ResourcePermission.WRITE, False),
            "PATCH": (ResourcePermission.WRITE, False),
        }.get(method, (ResourcePermission.READ, False))
        try:
            env = self._pick_env(inp.environment, inp.url)
            plan.environment = env
            scope = VariableScope(
                env,
                inp.variables,
                extra_secrets=inp.secret_variables,
                secret_loader=lambda _ref: _PLACEHOLDER,
            )
            text = scope.interpolate(self._join_base(inp.url, env)).text
            plan.url = parse_url(text)
        except (HttpFailure, VariableError, PolicyViolation) as exc:
            plan.problems.append(str(getattr(exc, "message", exc)))
            return plan
        plan.destination = self.policy.classify(plan.url)
        permission, destructive = plan.permission, plan.destructive
        if permission is ResourcePermission.READ:
            tokens = set(_WORD.findall(plan.url.path.lower()))
            hit = sorted(tokens & _STATE_WORDS)
            if hit:
                permission = ResourcePermission.WRITE
                plan.reasons.append(f"{method} {plan.url.path} looks state-changing ('{hit[0]}')")
        plan.permission, plan.destructive = permission, destructive
        env = plan.environment
        dest = plan.destination
        changes = permission is not ResourcePermission.READ
        if changes and env is not None and env.production and plan.url.origin in env.origins:
            plan.high_risk = True
            plan.reasons.append(f"changes the production environment '{env.name}'")
        if dest.sensitive:
            plan.high_risk = True
            plan.reasons.append("; ".join(dest.notes))
        if env is not None and not env.verify_tls and plan.url.origin in env.origins:
            plan.high_risk = True
            plan.reasons.append("TLS certificate checking is disabled for this environment")
        if changes and not dest.trusted and dest.kind is not Kind.LOOPBACK:
            plan.high_risk = True
            plan.reasons.append("changes a remote service that is not a trusted origin")
        body = inp.body or {}
        for field_value in (body.get("multipart") or {}).values():
            if isinstance(field_value, str) and field_value.startswith("@"):
                plan.uploads.append(field_value[1:][:80])
        if plan.uploads and not dest.trusted:
            plan.high_risk = True
            plan.reasons.append("uploads a workspace file to a destination that is not trusted")
        return plan

    def _collection_plans(self, params: dict[str, Any]) -> list[Plan]:
        try:
            collection = self._load_collection(str(params.get("name", "")))
            requests = self._select_requests(collection, params.get("request"))
        except (HttpFailure, CollectionError):
            return []
        plans = []
        for request in requests:
            inp = self._collection_input(collection, request, params.get("environment"))
            plans.append(self._plan_input(inp))
        return plans

    # ── Inputs ───────────────────────────────────────────────────────────────

    def _pick_env(self, name: str | None, url: str = "") -> Environment | None:
        if name:
            env = self.environments.get(str(name))
            if env is None:
                available = ", ".join(sorted(self.environments)) or "none configured"
                problem = self.environment_problems.get(str(name))
                raise HttpFailure(
                    ErrorCode.VALIDATION_FAILED,
                    problem or f"Unknown environment '{str(name)[:40]}'. Available: {available}.",
                )
            return env
        default = self.config.default_environment
        if default and default in self.environments:
            return self.environments[default]
        if len(self.environments) == 1:
            return next(iter(self.environments.values()))
        if (url.startswith("/") or "{{" in url) and self.environments:
            raise HttpFailure(
                ErrorCode.VALIDATION_FAILED,
                "Several environments exist; name one with 'environment'. Available: "
                + ", ".join(sorted(self.environments)),
            )
        return None

    @staticmethod
    def _join_base(url: str, env: Environment | None) -> str:
        text = url.strip()
        if text.startswith("/") and not text.startswith("//"):
            if env is None:
                raise HttpFailure(
                    ErrorCode.VALIDATION_FAILED,
                    "A relative URL needs an environment; none is selected or configured.",
                )
            return env.base_url + text
        return text

    @staticmethod
    def _pairs(values: Any, sep: str, what: str) -> list[tuple[str, str]]:
        if values is None:
            return []
        if not isinstance(values, list):
            raise HttpFailure(ErrorCode.VALIDATION_FAILED, f"{what} must be a list of text items.")
        out = []
        for item in values:
            if not isinstance(item, str) or sep not in item:
                raise HttpFailure(
                    ErrorCode.VALIDATION_FAILED,
                    f"Each {what} item must look like 'name{sep}value'.",
                )
            key, _, value = item.partition(sep)
            key = key.strip()
            if not key:
                raise HttpFailure(ErrorCode.VALIDATION_FAILED, f"A {what} item has no name.")
            out.append((key, value.strip() if sep == ":" else value))
        return out

    def _request_input(self, params: dict[str, Any], *, method: str | None = None) -> RequestInput:
        verb = (method or str(params.get("method", "GET"))).upper()
        if verb not in METHODS:
            raise HttpFailure(ErrorCode.VALIDATION_FAILED, f"Unsupported method '{verb[:12]}'.")
        url = params.get("url")
        if not isinstance(url, str) or not url.strip():
            raise HttpFailure(ErrorCode.INVALID_URL, "A url is required.")
        body = self._body_from_params(params)
        try:
            assertions = parse_assertions(params.get("expect"))
        except AssertionSyntaxError as exc:
            raise HttpFailure(ErrorCode.VALIDATION_FAILED, f"Invalid assertion: {exc}") from None
        auth = params.get("auth")
        if auth is not None and auth != "none" and not valid_reference(auth):
            raise HttpFailure(
                ErrorCode.VALIDATION_FAILED, "auth must be a credential reference name."
            )
        return RequestInput(
            method=verb,
            url=url,
            query=self._pairs(params.get("query"), "=", "query"),
            headers=self._pairs(params.get("headers"), ":", "headers"),
            body=body,
            auth=auth,
            environment=params.get("environment"),
            timeout=params.get("timeout"),
            assertions=assertions,
            select=[str(s) for s in (params.get("select") or [])][:20],
        )

    def _body_from_params(self, params: dict[str, Any]) -> dict[str, Any] | None:
        given = [
            k for k in ("json", "form", "multipart", "body") if params.get(k) not in (None, "", [])
        ]
        if len(given) > 1:
            raise HttpFailure(
                ErrorCode.VALIDATION_FAILED, "Give only one of json, form, multipart or body."
            )
        if not given:
            return None
        kind = given[0]
        if kind == "json":
            text = str(params["json"])
            if len(text) > MAX_BODY_JSON_CHARS:
                raise HttpFailure(ErrorCode.VALIDATION_FAILED, "The JSON body is too large.")
            try:
                return {"json": json.loads(text)}
            except ValueError:
                raise HttpFailure(
                    ErrorCode.VALIDATION_FAILED, "The json body is not valid JSON."
                ) from None
        if kind in ("form", "multipart"):
            return {kind: dict(self._pairs(params[kind], "=", kind))}
        content_type = params.get("content_type")
        if not isinstance(content_type, str) or not content_type.strip():
            raise HttpFailure(ErrorCode.VALIDATION_FAILED, "A raw body needs a content_type.")
        return {"text": str(params["body"]), "content_type": content_type.strip()}

    def _collection_input(
        self, collection: Collection, request: CollectionRequest, environment: str | None
    ) -> RequestInput:
        headers = [*collection.headers, *request.headers]
        auth = request.auth if request.auth is not None else collection.auth
        return RequestInput(
            method=request.method,
            url=request.url,
            query=list(request.query),
            headers=list(headers),
            body=request.body,
            auth=auth,
            environment=environment or collection.environment,
            timeout=request.timeout,
            assertions=list(request.assertions),
            variables=dict(collection.variables),
            extract=request.extract,
        )

    def _openapi_input(self, params: dict[str, Any], *, validate: bool) -> RequestInput:
        catalog = self._spec(str(params.get("spec", "")))
        endpoint = catalog.endpoints.get(str(params.get("operation", "")))
        if endpoint is None:
            raise HttpFailure(
                ErrorCode.NOT_FOUND,
                f"No operation '{str(params.get('operation', ''))[:40]}' in '{catalog.name}'.",
                hint="Use openapi_search to find operation ids.",
            )
        path_values = dict(self._pairs(params.get("path_params"), "=", "path_params"))
        query = self._pairs(params.get("query"), "=", "query")
        headers = self._pairs(params.get("headers"), ":", "headers")
        body = self._body_from_params(params)
        if validate:
            document = None
            if body and "json" in body:
                document = body["json"]
            elif body and "form" in body:
                document = dict(body["form"])
            problems = oa.check_call(
                endpoint,
                path=path_values,
                query=dict(query),
                headers=dict(headers),
                body=document,
                has_body=body is not None,
            )
            if problems:
                raise HttpFailure(
                    ErrorCode.VALIDATION_FAILED,
                    "The request does not match the specification: " + "; ".join(problems),
                )
        missing = [
            p.name
            for p in endpoint.parameters
            if p.location == "path" and p.name not in path_values
        ]
        if missing:
            raise HttpFailure(
                ErrorCode.VALIDATION_FAILED, f"Missing path parameter(s): {', '.join(missing)}."
            )
        path = oa.fill_path(endpoint.path, path_values)
        env_name = params.get("environment")
        env = (
            self._pick_env(env_name, "/")
            if env_name or self.config.default_environment or len(self.environments) == 1
            else None
        )
        if env is not None:
            base = "{{base_url}}"
        else:
            base = self._spec_server(catalog)
        try:
            assertions = parse_assertions(params.get("expect"))
        except AssertionSyntaxError as exc:
            raise HttpFailure(ErrorCode.VALIDATION_FAILED, f"Invalid assertion: {exc}") from None
        return RequestInput(
            method=endpoint.method,
            url=f"{base}{path}",
            query=query,
            headers=headers,
            body=body,
            auth=params.get("auth"),
            environment=env.name if env else None,
            timeout=params.get("timeout"),
            assertions=assertions,
            select=[str(s) for s in (params.get("select") or [])][:20],
        )

    @staticmethod
    def _spec_server(catalog: oa.ApiCatalog) -> str:
        for server in catalog.servers:
            if server.startswith(("http://", "https://")):
                return server.rstrip("/")
        if catalog.source.startswith(("http://", "https://")):
            try:
                return str(parse_url(catalog.source).origin)
            except PolicyViolation:
                pass
        raise HttpFailure(
            ErrorCode.VALIDATION_FAILED,
            "The specification names no usable server; pass an environment.",
        )

    def _spec(self, name: str) -> oa.ApiCatalog:
        catalog = self._specs.get(name)
        if catalog is None:
            loaded = ", ".join(self._specs) or "none loaded"
            raise HttpFailure(
                ErrorCode.NOT_FOUND,
                f"No specification named '{name[:40]}'. Loaded: {loaded}.",
                hint="Load one with openapi_load.",
            )
        self._specs.move_to_end(name)
        return catalog

    # ── Collections: loading and authorization ───────────────────────────────

    def _need_store(self) -> CollectionStore:
        if self._store is None:
            raise HttpFailure(ErrorCode.UNAVAILABLE, "Collections need a workspace.")
        return self._store

    def _load_collection(self, name: str) -> Collection:
        try:
            return self._need_store().load(name)
        except CollectionError as exc:
            raise HttpFailure(ErrorCode.NOT_FOUND, str(exc)) from None

    @staticmethod
    def _select_requests(collection: Collection, only: Any) -> list[CollectionRequest]:
        if only:
            request = collection.request(str(only))
            if request is None:
                raise HttpFailure(
                    ErrorCode.NOT_FOUND,
                    f"No request '{str(only)[:40]}' in '{collection.name}'. "
                    f"Requests: {', '.join(r.id for r in collection.requests[:20])}.",
                )
            return [request]
        return list(collection.requests)

    def _run_key(self, collection: Collection, params: dict[str, Any]) -> str:
        return fingerprint(
            collection,
            str(params.get("environment") or ""),
            str(params.get("request") or ""),
            json.dumps(sorted(self.environments)),
        )

    def _remember_authorization(self, params: dict[str, Any]) -> None:
        try:
            collection = self._load_collection(str(params.get("name", "")))
        except HttpFailure:
            return
        self._authorized[self._run_key(collection, params)] = time.monotonic()
        while len(self._authorized) > 64:
            self._authorized.popitem(last=False)

    # ── Execution ────────────────────────────────────────────────────────────

    async def execute(self, action: str, params: dict[str, Any] | None = None) -> ResourceResult:
        params = dict(params or {})
        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            return ResourceResult.failure(
                f"Unsupported action '{action}'.",
                action=action,
                code=ErrorCode.UNSUPPORTED_OPERATION,
            )
        try:
            return await handler(params)
        except HttpFailure as failure:
            return self._failure(action, failure)
        except (CredentialError, VariableError) as exc:
            code = (
                ErrorCode.AUTHENTICATION_FAILED
                if isinstance(exc, CredentialError)
                else ErrorCode.VALIDATION_FAILED
            )
            return ResourceResult.failure(str(exc), action=action, code=code)

    @staticmethod
    def _failure(action: str, failure: HttpFailure) -> ResourceResult:
        message = failure.message
        if failure.redirects:
            hops = " → ".join(
                str(h.get("to") or h.get("from")) for h in redirect_entries(failure.redirects)[:6]
            )
            message += f" Redirect chain: {hops}."
        return ResourceResult.failure(message, action=action, code=failure.code, hint=failure.hint)

    async def _do_request(self, params: dict[str, Any]) -> ResourceResult:
        inp = self._request_input(params)
        result, _summary = await self._run(inp, "request")
        return result

    async def _do_openapi_call(self, params: dict[str, Any]) -> ResourceResult:
        inp = self._openapi_input(params, validate=True)
        result, _summary = await self._run(inp, "openapi_call")
        return result

    async def _run(
        self, inp: RequestInput, action: str, *, scope: VariableScope | None = None
    ) -> tuple[ResourceResult, dict[str, Any]]:
        """Resolve, authenticate, send and present one request."""
        env = self._pick_env(inp.environment, inp.url)
        if scope is None:
            scope = VariableScope(env, inp.variables, extra_secrets=inp.secret_variables)
        used_secret = False

        def fill(text: str) -> str:
            nonlocal used_secret
            done = scope.interpolate(text)
            used_secret = used_secret or done.used_secret
            return done.text

        try:
            parsed = parse_url(fill(self._join_base(inp.url, env)))
        except PolicyViolation as exc:
            raise HttpFailure(
                ErrorCode.INVALID_URL if exc.code == "invalid_url" else ErrorCode.NETWORK_POLICY,
                exc.message,
            ) from None
        query = [(k, fill(v)) for k, v in inp.query]
        headers: list[tuple[str, str]] = []
        in_env = env is not None and parsed.origin in env.origins
        if env is not None and in_env:
            headers += list(env.headers.items())
        headers += [(k, fill(v)) for k, v in inp.headers]
        body_bytes, content_type = self._encode_body(inp.body, fill, parsed)
        if content_type and not any(k.lower() == "content-type" for k, _ in headers):
            headers.append(("Content-Type", content_type))
        if used_secret and not in_env:
            raise HttpFailure(
                ErrorCode.VALIDATION_FAILED,
                "A secret variable may only be sent to its own environment's origin "
                f"({env.origin if env else 'no environment selected'}); this request goes to {parsed.origin}.",
            )
        applied_query: list[tuple[str, str]] = []
        injected: frozenset[str] = frozenset()
        warnings: list[str] = []
        credential_ref = (
            inp.auth if inp.auth is not None else (env.auth if env and in_env else None)
        )
        if credential_ref and credential_ref != "none":
            explicit = inp.auth is not None
            try:
                credential = load_credential(credential_ref)
                token = None
                if credential.kind == "oauth2_client_credentials":
                    if credential.origin != parsed.origin:
                        raise CredentialError(
                            f"credential '{credential_ref}' is bound to {credential.origin} and is not sent to {parsed.origin}"
                        )
                    token = await oauth2_token(credential, self._tokens, self._fetch_token)
                applied = apply_credential(credential, parsed.origin, token=token)
                headers = [(k, v) for k, v in headers if k.lower() not in applied.header_names]
                headers += applied.headers
                applied_query = applied.query
                injected = frozenset(applied.header_names)
                warnings += applied.warnings
            except CredentialError as exc:
                if explicit:
                    raise HttpFailure(ErrorCode.AUTHENTICATION_FAILED, str(exc)) from None
                warnings.append(f"credentials were not sent: {exc}")
        request = OutRequest(
            method=inp.method,
            url=parsed,
            headers=headers,
            query=[*query, *applied_query],
            body=body_bytes,
            injected_headers=injected,
            follow_redirects=inp.follow_redirects,
            deadline=float(min(inp.timeout, MAX_DEADLINE)) if inp.timeout else None,
            verify_tls=env.verify_tls if (env is not None and in_env) else True,
        )
        outcome = await self._send(request)
        result, summary = self._present(outcome, inp, env, warnings, action)
        self._extract(inp, outcome, scope)
        return result, summary

    async def _send(self, request: OutRequest) -> HttpOutcome:
        return await self.executor.send(request)

    async def _fetch_token(
        self, url: str, form: dict[str, str], basic: tuple[str, str]
    ) -> tuple[int, Any]:
        import base64

        parsed = parse_url(url)
        raw = f"{basic[0]}:{basic[1]}".encode()
        request = OutRequest(
            method="POST",
            url=parsed,
            headers=[
                ("Authorization", "Basic " + base64.b64encode(raw).decode("ascii")),
                ("Content-Type", "application/x-www-form-urlencoded"),
                ("Accept", "application/json"),
            ],
            body=urlencode(form).encode(),
            follow_redirects=False,
        )
        outcome = await self.executor.send(request)
        try:
            return outcome.status, json.loads(outcome.body.decode("utf-8", "replace"))
        except ValueError:
            return outcome.status, None

    def _encode_body(
        self, body: dict[str, Any] | None, fill: Any, parsed: ParsedUrl
    ) -> tuple[bytes | None, str | None]:
        if not body:
            return None, None
        if "json" in body:
            document = _interpolate_json(body["json"], fill)
            return json.dumps(document, separators=(",", ":")).encode(), "application/json"
        if "form" in body:
            fields = {k: fill(v) for k, v in body["form"].items()}
            return urlencode(fields).encode(), "application/x-www-form-urlencoded"
        if "text" in body:
            return fill(body["text"]).encode(), body["content_type"]
        return self._encode_multipart(body["multipart"], fill)

    def _encode_multipart(self, fields: dict[str, str], fill: Any) -> tuple[bytes, str]:
        import httpx

        data: dict[str, str] = {}
        files: dict[str, tuple[str, bytes, str]] = {}
        for key, raw in fields.items():
            if raw.startswith("@"):
                path = self._upload_path(raw[1:])
                files[key] = (path.name, path.read_bytes(), "application/octet-stream")
            else:
                data[key] = fill(raw)
        request = httpx.Request("POST", "http://upload.invalid/", data=data, files=files or None)
        content = request.read()
        return content, request.headers["content-type"]

    def _upload_path(self, relative: str) -> Path:
        if self.workspace is None:
            raise HttpFailure(ErrorCode.VALIDATION_FAILED, "File uploads need a workspace.")
        try:
            path = (self.workspace / relative).resolve()
            path.relative_to(self.workspace)
        except (ValueError, OSError):
            raise HttpFailure(
                ErrorCode.VALIDATION_FAILED, "An uploaded file must be inside the workspace."
            ) from None
        from velune.permissions.boundary import is_secret_path

        if is_secret_path(path):
            raise HttpFailure(
                ErrorCode.VALIDATION_FAILED, "That file looks like a secret and is not uploaded."
            )
        if not path.is_file():
            raise HttpFailure(ErrorCode.NOT_FOUND, f"No such file to upload: {relative[:80]}")
        if path.stat().st_size > self.limits.max_request_bytes:
            raise HttpFailure(
                ErrorCode.VALIDATION_FAILED, "The file is larger than the request size limit."
            )
        return path

    # ── Presenting ───────────────────────────────────────────────────────────

    def _present(
        self,
        outcome: HttpOutcome,
        inp: RequestInput,
        env: Environment | None,
        warnings: list[str],
        action: str,
    ) -> tuple[ResourceResult, dict[str, Any]]:
        view, _text = view_of(outcome)
        fields, document, _ = present_body(outcome, self._redact_fields)
        status_class = status_outcome(outcome.status)
        data: dict[str, Any] = {
            "request_id": outcome.request_id,
            "method": inp.method,
            "url": redact_url(outcome.url),
            "environment": env.name if env else None,
            "status": outcome.status,
            "reason": outcome.reason,
            "outcome": status_class,
            "duration_ms": round(outcome.duration_ms, 1),
            "headers": clean_headers(outcome.headers),
            **fields,
        }
        if outcome.redirects:
            data["redirects"] = redirect_entries(outcome.redirects)
        if inp.select and document is not None:
            data["selected"] = select_paths(document, inp.select)
        elif inp.select:
            data["selected"] = "the response is not JSON"
        summary: dict[str, Any] = {"status": outcome.status, "outcome": status_class}
        if inp.assertions:
            verdict = evaluate(inp.assertions, view)
            data["assertions"] = verdict
            summary["assertions_passed"] = verdict["passed"]
            summary["failed"] = [r for r in verdict["results"] if not r["passed"]]
            if not verdict["passed"]:
                data["outcome"] = "assertion_failed"
        result = ResourceResult.success(action, data)
        result.warnings = [*warnings, *outcome.warnings]
        summary["duration_ms"] = data["duration_ms"]
        return result, summary

    def _extract(self, inp: RequestInput, outcome: HttpOutcome, scope: VariableScope) -> None:
        """Store values a collection request extracts, for later requests in the run."""
        if not inp.extract:
            return
        view, text = view_of(outcome)
        document = view.json()
        for rule in inp.extract:
            value: Any = None
            if "json" in rule:
                found = json_lookup(document, rule["json"]) if document is not None else None
                value = None if found is None or is_missing(found) else found
            elif "header" in rule:
                value = view.headers.get(str(rule["header"]).lower())
            if value is None:
                continue
            text_value = value if isinstance(value, str) else json.dumps(value)
            if rule.get("secret"):
                register_secret_value(text_value)
                inp.secret_variables = frozenset({*inp.secret_variables, rule["var"]})
            inp.variables[rule["var"]] = text_value

    # ── Other operations ─────────────────────────────────────────────────────

    async def _do_diagnose(self, params: dict[str, Any]) -> ResourceResult:
        inp = self._request_input(params, method=str(params.get("method") or "HEAD"))
        inp.follow_redirects = True
        stages: list[dict[str, Any]] = []
        env = self._pick_env(inp.environment, inp.url)
        scope = VariableScope(env, secret_loader=lambda _ref: _PLACEHOLDER)
        try:
            parsed = parse_url(scope.interpolate(self._join_base(inp.url, env)).text)
        except (PolicyViolation, VariableError) as exc:
            stages.append(
                {"stage": "url", "ok": False, "detail": str(getattr(exc, "message", exc))}
            )
            return self._diagnosis(stages, "The URL is not valid.", ErrorCode.INVALID_URL)
        dest = self.policy.classify(parsed)
        stages.append(
            {
                "stage": "url",
                "ok": not dest.blocked,
                "detail": dest.blocked
                or f"{parsed.scheme} to {parsed.host}:{parsed.port} ({dest.kind.value})",
            }
        )
        if dest.blocked:
            return self._diagnosis(
                stages, "The network policy forbids this destination.", ErrorCode.NETWORK_POLICY
            )
        started = time.perf_counter()
        try:
            addresses = await self.policy.resolve(parsed.host, parsed.port, self.executor.resolver)
            stages.append(
                {
                    "stage": "dns",
                    "ok": True,
                    "detail": f"{len(addresses)} address(es)",
                    "ms": round((time.perf_counter() - started) * 1000, 1),
                }
            )
        except PolicyViolation as exc:
            stages.append({"stage": "dns", "ok": False, "detail": exc.message})
            code = ErrorCode.DNS_FAILED if exc.code == "dns_failed" else ErrorCode.NETWORK_POLICY
            return self._diagnosis(stages, exc.message, code)
        try:
            result, summary = await self._run(inp, "diagnose")
        except HttpFailure as failure:
            stage = {
                ErrorCode.TLS_FAILED: "tls",
                ErrorCode.TIMEOUT: "connect-or-read",
            }.get(failure.code, "connect")
            stages.append({"stage": stage, "ok": False, "detail": failure.message})
            return self._diagnosis(stages, failure.hint or failure.message, failure.code)
        stages.append(
            {
                "stage": "http",
                "ok": True,
                "detail": f"HTTP {summary['status']} ({summary['outcome']})",
            }
        )
        advice = {
            "success": "The API responded normally.",
            "redirect": "The API redirected; follow the Location header.",
            "authentication_failed": "The server rejected the credentials: check the credential and its origin.",
            "authorization_denied": "Authenticated but not allowed: check the account's permissions.",
            "client_error": "The server rejected the request: check the path and parameters.",
            "server_error": "The server failed while handling the request: check its logs.",
        }.get(summary["outcome"], "")
        data = {
            "stages": stages,
            "diagnosis": advice,
            "status": summary["status"],
            "outcome": summary["outcome"],
            "duration_ms": summary["duration_ms"],
        }
        return ResourceResult.success("diagnose", data)

    @staticmethod
    def _diagnosis(stages: list[dict[str, Any]], message: str, code: ErrorCode) -> ResourceResult:
        failed = next((s["stage"] for s in stages if not s["ok"]), "unknown")
        failure = ResourceResult.failure(
            f"The request fails at the '{failed}' step: {message}",
            action="diagnose",
            code=code,
            hint="; ".join(f"{s['stage']}: {'ok' if s['ok'] else s['detail']}" for s in stages)[
                :300
            ],
        )
        return failure

    async def _do_environments(self, params: dict[str, Any]) -> ResourceResult:
        wanted = params.get("name")
        if wanted:
            env = self.environments.get(str(wanted))
            if env is None:
                raise HttpFailure(
                    ErrorCode.NOT_FOUND,
                    self.environment_problems.get(
                        str(wanted), f"No environment '{str(wanted)[:40]}'."
                    ),
                )
            return ResourceResult.success("environments", {"environment": env.summary()})
        data: dict[str, Any] = {
            "environments": [e.summary() for e in self.environments.values()],
            "default": self.config.default_environment,
            "trusted_origins": sorted(str(o) for o in self.policy.trusted_origins),
        }
        if self.environment_problems:
            data["problems"] = dict(self.environment_problems)
        if not self.workspace_trusted:
            data["note"] = "this workspace is not trusted, so configured origins are ignored"
        return ResourceResult.success("environments", data)

    async def _do_collection_list(self, params: dict[str, Any]) -> ResourceResult:
        store = self._need_store()
        entries = []
        for name in store.names():
            try:
                c = store.load(name)
                entries.append(
                    {"name": name, "requests": len(c.requests), "description": c.description[:120]}
                )
            except CollectionError as exc:
                entries.append({"name": name, "error": str(exc)})
        return ResourceResult.success("collection_list", {"collections": entries})

    async def _do_collection_show(self, params: dict[str, Any]) -> ResourceResult:
        collection = self._load_collection(str(params.get("name", "")))
        data = collection_to_dict(collection)
        return ResourceResult.success("collection_show", _mask_collection(data))

    async def _do_collection_run(self, params: dict[str, Any]) -> ResourceResult:
        collection = self._load_collection(str(params.get("name", "")))
        requests = self._select_requests(collection, params.get("request"))
        key = self._run_key(collection, params)
        if key not in self._authorized:
            raise HttpFailure(
                ErrorCode.AUTHORIZATION_DENIED,
                "The collection was not authorized as it is now (it may have changed since it was "
                "approved). Run it again.",
            )
        stop = bool(params.get("stop_on_failure"))
        scope_vars: dict[str, str] = {}
        secret_names: set[str] = set()
        rows: list[dict[str, Any]] = []
        passed = failed = errored = 0
        started = time.perf_counter()
        for request in requests:
            inp = self._collection_input(collection, request, params.get("environment"))
            inp.variables.update(scope_vars)
            inp.secret_variables = frozenset(secret_names)
            row: dict[str, Any] = {"id": request.id, "name": request.name, "method": request.method}
            try:
                env = self._pick_env(inp.environment, inp.url)
                scope = VariableScope(env, inp.variables, extra_secrets=inp.secret_variables)
                result, summary = await self._run(inp, "collection_run", scope=scope)
                row.update(summary)
                row["url"] = result.data.get("url") if isinstance(result.data, dict) else None
                row["ok"] = bool(
                    summary.get(
                        "assertions_passed", summary.get("outcome") in ("success", "redirect")
                    )
                )
                passed, failed = (passed + 1, failed) if row["ok"] else (passed, failed + 1)
                if params.get("request"):
                    row["response"] = result.data  # one request asked for: the whole response
                if not row["ok"] and isinstance(result.data, dict):
                    preview = (
                        result.data.get("text")
                        or result.data.get("json")
                        or result.data.get("json_preview")
                    )
                    if preview is not None:
                        row["body_preview"] = str(preview)[:300]
            except (HttpFailure, VariableError, CredentialError) as exc:
                errored += 1
                row.update(ok=False, error=str(getattr(exc, "message", exc))[:300])
                if isinstance(exc, HttpFailure):
                    row["code"] = exc.code.value
            scope_vars.update(inp.variables)
            secret_names.update(inp.secret_variables)
            rows.append(row)
            if stop and not row.get("ok"):
                break
        failures = [r for r in rows if not r.get("ok")]
        data = {
            "collection": collection.name,
            "total": len(requests),
            "ran": len(rows),
            "passed": passed,
            "failed": failed,
            "errors": errored,
            "all_passed": not failures and len(rows) == len(requests),
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "results": rows,
            "failed_requests": [
                {
                    k: r.get(k)
                    for k in ("id", "name", "status", "outcome", "error", "code", "failed")
                    if r.get(k) is not None
                }
                for r in failures
            ],
        }
        return ResourceResult.success("collection_run", data)

    def _workspace_file(self, raw: Any, *, must_exist: bool) -> Path:
        if self.workspace is None:
            raise HttpFailure(ErrorCode.UNAVAILABLE, "This needs a workspace.")
        if not isinstance(raw, str) or not raw.strip():
            raise HttpFailure(ErrorCode.VALIDATION_FAILED, "A file path is required.")
        try:
            path = (self.workspace / raw).resolve()
            path.relative_to(self.workspace)
        except (ValueError, OSError):
            raise HttpFailure(
                ErrorCode.VALIDATION_FAILED, "The file must be inside the workspace."
            ) from None
        if must_exist and not path.is_file():
            raise HttpFailure(ErrorCode.NOT_FOUND, f"No such file: {raw[:80]}")
        return path

    async def _do_collection_import(self, params: dict[str, Any]) -> ResourceResult:
        path = self._workspace_file(params.get("file"), must_exist=True)
        if path.stat().st_size > 5 * 1024 * 1024:
            raise HttpFailure(ErrorCode.VALIDATION_FAILED, "The file is larger than 5 MiB.")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise HttpFailure(ErrorCode.VALIDATION_FAILED, "The file is not valid JSON.") from None
        wanted = params.get("name")
        if wanted and not valid_name(wanted):
            raise HttpFailure(
                ErrorCode.VALIDATION_FAILED,
                "The name must be lowercase letters, digits, '-' or '_'.",
            )
        limit = self.config.max_collection_requests
        skipped: list[str] = []
        try:
            if params.get("format") == "postman":
                imported = from_postman(document, name=wanted, max_requests=limit)
                collection, skipped = imported.collection, imported.skipped
            else:
                if wanted and isinstance(document, dict):
                    document = {**document, "name": wanted}
                collection = parse_collection(document, max_requests=limit)
            saved = self._need_store().save(collection, overwrite=bool(params.get("overwrite")))
        except CollectionError as exc:
            raise HttpFailure(ErrorCode.VALIDATION_FAILED, str(exc)) from None
        data: dict[str, Any] = {
            "name": collection.name,
            "requests": len(collection.requests),
            "saved_to": str(saved.relative_to(self.workspace)) if self.workspace else str(saved),
        }
        if skipped:
            data["skipped"] = skipped[:20]
        return ResourceResult.success("collection_import", data)

    async def _do_collection_export(self, params: dict[str, Any]) -> ResourceResult:
        collection = self._load_collection(str(params.get("name", "")))
        path = self._workspace_file(params.get("file"), must_exist=False)
        if path.suffix.lower() != ".json":
            raise HttpFailure(ErrorCode.VALIDATION_FAILED, "The destination must be a .json file.")
        if path.exists() and not params.get("overwrite"):
            raise HttpFailure(
                ErrorCode.CONFLICT, "That file already exists; pass overwrite to replace it."
            )
        from velune.permissions.boundary import is_secret_path, protected_reason

        if is_secret_path(path) or protected_reason(path):
            raise HttpFailure(ErrorCode.VALIDATION_FAILED, "That destination is not allowed.")
        document = (
            to_postman(collection)
            if params.get("format") == "postman"
            else collection_to_dict(collection)
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        return ResourceResult.success(
            "collection_export",
            {
                "file": str(path.relative_to(self.workspace)),
                "format": params.get("format") or "velune",
                **(
                    {"note": "Postman export omits assertions and extraction rules"}
                    if params.get("format") == "postman"
                    else {}
                ),
            },
        )

    async def _do_openapi_load(self, params: dict[str, Any]) -> ResourceResult:
        if bool(params.get("file")) == bool(params.get("url")):
            raise HttpFailure(ErrorCode.VALIDATION_FAILED, "Give exactly one of file or url.")
        if params.get("file"):
            path = self._workspace_file(params["file"], must_exist=True)
            if path.stat().st_size > oa.MAX_SPEC_BYTES:
                raise HttpFailure(
                    ErrorCode.VALIDATION_FAILED, "The specification file is too large."
                )
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                raise HttpFailure(
                    ErrorCode.VALIDATION_FAILED, "The file could not be read as text."
                ) from None
            source = str(path.relative_to(self.workspace))
        else:
            text, source = await self._fetch_spec(params)
        try:
            title_guess = params.get("name") or ""
            catalog = oa.parse_spec(text, name=title_guess or "spec", source=source)
        except oa.OpenApiError as exc:
            raise HttpFailure(ErrorCode.VALIDATION_FAILED, str(exc)) from None
        name = params.get("name") or _slug(catalog.title) or "spec"
        if not _NAME.match(name):
            raise HttpFailure(
                ErrorCode.VALIDATION_FAILED,
                "The name must be lowercase letters, digits, '-' or '_'.",
            )
        catalog.name = name
        self._specs[name] = catalog
        self._specs.move_to_end(name)
        while len(self._specs) > MAX_SPECS:
            self._specs.popitem(last=False)
        return ResourceResult.success("openapi_load", catalog.summary())

    async def _fetch_spec(self, params: dict[str, Any]) -> tuple[str, str]:
        inp = RequestInput(
            "GET",
            str(params["url"]),
            headers=[("Accept", "application/json, application/yaml, text/yaml, */*")],
            environment=params.get("environment"),
            auth="none",
        )
        env = self._pick_env(inp.environment, inp.url)
        scope = VariableScope(env, secret_loader=lambda _ref: _PLACEHOLDER)
        try:
            parsed = parse_url(scope.interpolate(self._join_base(inp.url, env)).text)
        except (PolicyViolation, VariableError) as exc:
            raise HttpFailure(ErrorCode.INVALID_URL, str(getattr(exc, "message", exc))) from None
        executor = HttpExecutor(
            self.policy,
            replace(self.limits, max_response_bytes=oa.MAX_SPEC_BYTES),
            resolver=self.executor.resolver,
            on_connect=self.executor.on_connect,
            transport_factory=self.executor.transport_factory,
            ssl_context=self.executor.ssl_context,
        )
        outcome = await executor.send(OutRequest("GET", parsed, headers=list(inp.headers)))
        if outcome.status != 200:
            raise HttpFailure(
                ErrorCode.NOT_FOUND, f"The specification URL answered HTTP {outcome.status}."
            )
        return outcome.body.decode("utf-8", "replace"), redact_url(str(parsed))

    async def _do_openapi_search(self, params: dict[str, Any]) -> ResourceResult:
        catalog = self._spec(str(params.get("spec", "")))
        if params.get("operation"):
            endpoint = catalog.endpoints.get(str(params["operation"]))
            if endpoint is None:
                raise HttpFailure(
                    ErrorCode.NOT_FOUND, f"No operation '{str(params['operation'])[:40]}'."
                )
            return ResourceResult.success(
                "openapi_search", {"endpoint": oa.endpoint_detail(endpoint, catalog)}
            )
        limit = int(params.get("limit") or 15)
        found = oa.search(
            catalog,
            str(params.get("query") or ""),
            tag=str(params.get("tag") or ""),
            method=str(params.get("method") or ""),
            limit=limit,
        )
        return ResourceResult.success(
            "openapi_search",
            {
                "spec": catalog.name,
                "matches": [oa.endpoint_card(e) for e in found],
                "shown": len(found),
                "total_endpoints": len(catalog.endpoints),
            },
        )


# ── helpers ─────────────────────────────────────────────────────────────────


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48].strip("-")


def _interpolate_json(value: Any, fill: Any, depth: int = 0) -> Any:
    if depth > 32:
        raise HttpFailure(ErrorCode.VALIDATION_FAILED, "The JSON body is nested too deeply.")
    if isinstance(value, str):
        return fill(value) if "{{" in value else value
    if isinstance(value, dict):
        return {k: _interpolate_json(v, fill, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_json(v, fill, depth + 1) for v in value]
    return value


def _mask_collection(data: dict[str, Any]) -> dict[str, Any]:
    from velune.core.redaction import redact_structure

    return redact_structure(data)


__all__ = ["HttpConnector", "Plan", "RequestInput"]
