"""Supabase project connector (VAIR A5).

Talks to a Supabase project over its REST surface (PostgREST + Storage) with ``httpx``.
It does **not** open a PostgreSQL connection: SQL-level work on a Supabase database
belongs to the ``postgres`` integration, so nothing here duplicates it. What is
Supabase-specific lives here: the PostgREST table API (which Row Level Security
governs), the OpenAPI schema it publishes, Storage buckets, and RLS policy inspection.

Security posture:

* **RLS applies by default.** Requests use the project's anon key, so Row Level
  Security decides what comes back. The service-role key bypasses RLS: it is never
  shown to the model, and *using* it (``service_role=true``) is a privileged act that
  asks for confirmation in every mode and is never covered by a session grant.
* Only the configured project URL is ever contacted (validated up front), so this is
  not an SSRF primitive. Identifiers and the ``select`` clause are validated before
  they reach a path or a query string.
* Several projects can be configured by name (``connection="staging"``); keys live in
  the encrypted keystore with the project URL, never in configuration.
* Results are bounded, JSON-safe and column-redacted; errors never carry a key.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

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
from velune.resources.connections import (
    NamedConnections,
    Profile,
    _coerce_settings,
    _workspace_trusted,
)
from velune.resources.sql_results import MASK, column_is_sensitive, json_safe

logger = logging.getLogger("velune.resources.supabase")

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")
_SELECT = re.compile(r"^[A-Za-z0-9_,*() ]+$")
_TIMEOUT = 10.0
SCOPE_SERVICE_ROLE = "supabase.service_role.use"
SCOPE_SCHEMA_READ = "supabase.schema.read"
SCOPE_QUERY_READ = "supabase.query.read"
SCOPE_CONNECTION = "supabase.connection.read"
_FILTER_OPS = ("eq", "neq", "gt", "gte", "lt", "lte", "like", "ilike", "is")


def _http_error(status: int, text: str) -> tuple[ErrorCode, str]:
    """Map an HTTP status to a normalized code and a remediation hint."""
    if status == 401:
        return ErrorCode.AUTHENTICATION_FAILED, "Check the project's anon key."
    if status == 403:
        return (
            ErrorCode.AUTHORIZATION_DENIED,
            "Row Level Security or table privileges denied this request.",
        )
    if status in (404, 406):
        return ErrorCode.NOT_FOUND, "Check the table name (list_tables)."
    if status in (400, 416, 422):
        return ErrorCode.VALIDATION_FAILED, "Check the select, filter and order arguments."
    if status in (408, 504):
        return ErrorCode.TIMEOUT, "The project took too long to answer; narrow the request."
    if status == 429:
        return ErrorCode.UNAVAILABLE, "The project is rate limiting requests; retry later."
    if status >= 500:
        return ErrorCode.UNAVAILABLE, "The Supabase project reported a server error."
    return ErrorCode.ADAPTER_FAILURE, ""


class SupabaseConnector(NamedConnections, ResourceConnector):
    """Connector for Supabase Cloud (or self-hosted) projects."""

    resource_id = "supabase"
    display_name = "Supabase"
    description = (
        "Supabase projects over their REST API: tables (Row Level Security applies), "
        "schema, storage buckets and policies. For SQL use the postgres integration."
    )
    keywords = ("supabase", "postgrest", "storage", "bucket", "buckets", "edge", "rls")
    #: One short-lived httpx client per request, always closed.
    lifecycle = Lifecycle.PERSISTENT

    def __init__(
        self,
        name: str | None = None,
        config: dict[str, Any] | None = None,
        *,
        settings: Any = None,
        workspace: Any = None,
        workspace_trusted: bool | None = None,
    ) -> None:
        super().__init__(name)
        self._explicit_config = config
        self.settings = _coerce_settings(settings)
        self.workspace = workspace
        self._trusted = _workspace_trusted(workspace, workspace_trusted)
        self._state = ResourceState.DISCONNECTED
        self._error: str | None = None
        #: Base URL of the project a request is being made to.
        self._url: str | None = None
        self._info: dict[str, Any] = {}
        self._connected: dict[str, str] = {}  # connection name -> project URL

    # ── Config ───────────────────────────────────────────────────────────────

    def configuration_problem(self) -> str | None:
        if self.connection_names():
            return None
        return "Not configured — the user must run /resource configure supabase."

    @staticmethod
    def _validate_url(url: str) -> str | None:
        """Return a normalized https project URL, or None if unacceptable."""
        try:
            parsed = urlparse(url)
            port = parsed.port
        except ValueError:
            return None
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return None
        return f"https://{parsed.hostname}" + (f":{port}" if port else "")

    def _is_remote(self, stored: dict[str, Any]) -> bool:
        return True

    def driver_available(self) -> bool:
        try:
            import httpx  # noqa: F401

            return True
        except ImportError:
            return False

    def _client(self, config: dict[str, Any], *, service_role: bool = False):
        import httpx

        key = ""
        if service_role:
            key = config.get("service_role_key") or config.get("service_key") or ""
        if not key:
            key = config.get("anon_key") or config.get("key") or ""
        headers = {"apikey": key, "Authorization": f"Bearer {key}"} if key else {}
        base = config.get("_base_url") or self._url or ""
        return httpx.AsyncClient(base_url=base, headers=headers, timeout=_TIMEOUT)

    @staticmethod
    def _scrub_keys(config: dict[str, Any], text: str) -> str:
        """Remove this project's own keys from *text* (an error body can echo one back)."""
        for name in ("anon_key", "key", "service_role_key", "service_key"):
            value = config.get(name)
            if isinstance(value, str) and len(value) >= 8:
                text = text.replace(value, "***")
        return text

    def _has_service_role(self, config: dict[str, Any]) -> bool:
        return bool(config.get("service_role_key") or config.get("service_key"))

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def connect(self) -> ResourceResult:
        names = self.connection_names()
        default = self._default_name(names)
        if default is not None or len(names) <= 1:
            return await self._connect_named(default)
        results = [await self._connect_named(n) for n in names]
        bad = [n for n, r in zip(names, results, strict=True) if not r.ok]
        if bad:
            return ResourceResult.failure(
                f"Could not connect: {', '.join(bad)}.",
                action="connect",
                code=ErrorCode.CONNECTION_FAILED,
            )
        return ResourceResult.success("connect", {"connections": names})

    async def _connect_named(self, name: str | None) -> ResourceResult:
        if not self.driver_available():
            self._state = ResourceState.UNAVAILABLE
            self._error = "httpx is not installed; the Supabase connector is unavailable."
            return ResourceResult.failure(self._error, action="connect", code=ErrorCode.UNAVAILABLE)
        profile, failure = self._profile_for(name)
        if profile is None:
            self._state = ResourceState.DISCONNECTED
            assert failure is not None
            failure.action = "connect"
            return failure
        config = profile.config
        if not config.get("url"):
            self._state = ResourceState.DISCONNECTED
            return ResourceResult.failure(
                f"No project URL stored for Supabase '{profile.name}'. "
                "Configure a project URL and anon key first.",
                action="connect",
                code=ErrorCode.UNAVAILABLE,
            )
        url = self._validate_url(str(config["url"]))
        if url is None:
            self._state = ResourceState.ERROR
            self._error = "Invalid Supabase URL — must be an https:// project URL."
            return ResourceResult.failure(
                self._error, action="connect", code=ErrorCode.VALIDATION_FAILED
            )
        self._url = url
        self._state = ResourceState.CONNECTING
        verify = await self._verify({**config, "_base_url": url})
        if not verify.ok:
            self._state = ResourceState.ERROR
            self._error = verify.error
            return verify
        self._state = ResourceState.CONNECTED
        self._error = None
        self._connected[profile.name] = url
        self._info = {"url": url, "service_role": self._has_service_role(config)}
        return ResourceResult.success("connect", dict(self._info))

    async def disconnect(self) -> ResourceResult:
        self._state = ResourceState.DISCONNECTED
        self._url = None
        self._connected.clear()
        return ResourceResult.success("disconnect")

    def status(self) -> ResourceStatus:
        return ResourceStatus(
            resource_id=self.resource_id,
            display_name=self.display_name,
            state=self._state,
            detail=(urlparse(self._url).hostname if self._url else "") or "",
            info=dict(self._info),
            error=self._error,
        )

    async def discover(self) -> list[DiscoveryHint]:
        """A stored/configured Supabase project surfaces as a hint (no network)."""
        hints: list[DiscoveryHint] = []
        for name in self.connection_names():
            profile, _ = self._build_profile(name)
            url = profile.config.get("url") if profile else None
            if not url:
                continue
            try:
                host = urlparse(str(url)).hostname or ""
            except ValueError:
                host = ""
            hints.append(
                DiscoveryHint(
                    resource_id=self.resource_id,
                    display_name="Supabase project",
                    detail=host or "configured",
                    source="config",
                )
            )
        return hints

    async def probe_availability(self) -> tuple[bool, str]:
        if not self.driver_available():
            return False, "httpx is not installed"
        return (True, "") if self.connection_names() else (False, "not configured")

    # ── Capabilities ─────────────────────────────────────────────────────────

    def capabilities(self) -> list[ResourceCapability]:
        read = ResourcePermission.READ
        conn = {"type": "string", "description": "Named project (see list_connections)."}
        table = {"type": "string", "description": "Table name."}
        service = {
            "type": "boolean",
            "description": (
                "Use the privileged service-role key, which BYPASSES Row Level Security. "
                "Always needs the user's approval."
            ),
        }
        return [
            ResourceCapability(
                "list_connections",
                read,
                "List the configured Supabase projects (no keys)",
                scope=SCOPE_CONNECTION,
                category="connections",
            ),
            ResourceCapability(
                "verify",
                read,
                "Verify a project is reachable and the key works",
                scope=SCOPE_CONNECTION,
                category="connections",
                params={"properties": {"connection": conn}},
            ),
            ResourceCapability(
                "list_tables",
                read,
                "List tables exposed via PostgREST",
                scope=SCOPE_SCHEMA_READ,
                category="schema",
                params={"properties": {"connection": conn}},
            ),
            ResourceCapability(
                "inspect_schema",
                read,
                "Inspect a table's columns",
                scope=SCOPE_SCHEMA_READ,
                category="schema",
                params={
                    "properties": {"table": table, "connection": conn},
                    "required": ["table"],
                },
            ),
            ResourceCapability(
                "rls_policies",
                ResourcePermission.WRITE,
                "Inspect RLS policies (requires the service-role key; always asks)",
                scope=SCOPE_SERVICE_ROLE,
                category="schema",
                params={"properties": {"table": table, "connection": conn}},
            ),
            ResourceCapability(
                "query",
                read,
                "Read rows from a table, bounded; RLS applies (read-only)",
                scope=SCOPE_QUERY_READ,
                category="query_read",
                params={
                    "properties": {
                        "table": table,
                        "select": {
                            "type": "string",
                            "description": "Columns to select (default *).",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum rows (default 100).",
                            "minimum": 1,
                            "maximum": 1000,
                        },
                        "offset": {"type": "integer", "description": "Rows to skip.", "minimum": 0},
                        "order": {"type": "string", "description": "Column to order by."},
                        "descending": {"type": "boolean", "description": "Order high to low."},
                        "filter_column": {"type": "string", "description": "Column to filter on."},
                        "filter_op": {
                            "type": "string",
                            "description": "Filter operator.",
                            "enum": list(_FILTER_OPS),
                        },
                        "filter_value": {"type": "string", "description": "Value to compare."},
                        "service_role": service,
                        "connection": conn,
                    },
                    "required": ["table"],
                },
            ),
            ResourceCapability(
                "storage_buckets",
                read,
                "List Storage buckets",
                scope="supabase.storage.read",
                category="storage",
                params={"properties": {"service_role": service, "connection": conn}},
            ),
            ResourceCapability(
                "edge_functions",
                read,
                "List Edge Function metadata",
                scope="supabase.functions.read",
                category="functions",
                params={"properties": {"connection": conn}},
            ),
        ]

    def effective_permission(
        self, action: str, params: dict[str, Any] | None
    ) -> tuple[ResourcePermission, bool] | None:
        """Escalate any call that would use the service-role key.

        The service-role key bypasses RLS, so exercising it is a privileged act that
        must be confirmed regardless of which action requested it.
        """
        params = params or {}
        if params.get("service_role") or action == "rls_policies":
            return (ResourcePermission.WRITE, False)
        return None

    def scope_for(self, action, params, permission):
        if (params or {}).get("service_role") or action == "rls_policies":
            return SCOPE_SERVICE_ROLE
        return super().scope_for(action, params, permission)

    def describe_actions(self, action, params, *, ledger=None):
        """Service-role use is HIGH risk and touches a secret: it asks in every mode."""
        from velune.permissions.actions import Action as GateAction
        from velune.permissions.actions import ActionType, Risk
        from velune.resources.base import _safe_preview

        cap = self.capability_for(action)
        if cap is None:
            return []
        p = params or {}
        if p.get("service_role") or action == "rls_policies":
            return [
                GateAction(
                    ActionType.INTEGRATION_WRITE,
                    f"{self.name}:{action}",
                    "uses the service-role key, which bypasses Row Level Security",
                    Risk.HIGH,
                    secret=True,
                    detail=_safe_preview(self, action, p),
                    scope=SCOPE_SERVICE_ROLE,
                )
            ]
        return super().describe_actions(action, params, ledger=ledger)

    def authorization_preview(self, action: str, params: dict[str, Any] | None) -> str:
        params = params or {}
        target = params.get("table") or ""
        role = "service-role" if params.get("service_role") else "anon"
        project = params.get("connection") or ""
        return f"supabase {action} {target} [{role}]{' project=' + str(project) if project else ''}".strip()

    # ── Execution ────────────────────────────────────────────────────────────

    async def execute(self, action: str, params: dict[str, Any] | None = None) -> ResourceResult:
        params = params or {}
        if not self.driver_available():
            return ResourceResult.failure(
                "httpx is not installed.", action=action, code=ErrorCode.UNAVAILABLE
            )
        if action == "list_connections":
            return self._list_connections()
        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            return ResourceResult.failure(
                f"Unsupported action '{action}'.",
                action=action,
                code=ErrorCode.UNSUPPORTED_OPERATION,
            )
        name = params.get("connection")
        if name is not None and not isinstance(name, str):
            return ResourceResult.failure(
                "'connection' must be a string.", action=action, code=ErrorCode.VALIDATION_FAILED
            )
        profile, failure = self._profile_for(name)
        if profile is None:
            assert failure is not None
            failure.action = action
            if not self.connection_names():
                failure.error = "Supabase is not configured."
            return failure
        config = profile.config
        if not config.get("url"):
            return ResourceResult.failure(
                "Supabase is not configured.", action=action, code=ErrorCode.UNAVAILABLE
            )
        url = self._validate_url(str(config["url"]))
        if url is None:
            return ResourceResult.failure(
                "Invalid Supabase URL.", action=action, code=ErrorCode.VALIDATION_FAILED
            )
        self._url = url
        call = {**config, "_base_url": url, "_profile": profile.name}
        call_params = {**params, "_limits": profile}
        return await handler(call, call_params)

    def _list_connections(self) -> ResourceResult:
        rows: list[dict[str, Any]] = []
        default = self._default_name(self.connection_names())
        for name in self.connection_names():
            profile, reason = self._build_profile(name)
            if profile is None:
                rows.append({"name": name, "configured": False, "problem": reason[:160]})
                continue
            url = str(profile.config.get("url") or "")
            try:
                host = urlparse(url).hostname or ""
            except ValueError:
                host = ""
            entry: dict[str, Any] = {
                "name": name,
                "project": host,
                "service_role_available": self._has_service_role(profile.config),
                "state": (
                    ResourceState.CONNECTED
                    if name in self._connected
                    else ResourceState.DISCONNECTED
                ).value,
                "default": name == default,
            }
            if profile.environment:
                entry["environment"] = profile.environment
            rows.append(entry)
        return ResourceResult.success(
            "list_connections", {"connections": rows, "engine": self.resource_id}
        )

    async def _request(
        self,
        config: dict[str, Any],
        method: str,
        path: str,
        *,
        service_role: bool = False,
        **kw: Any,
    ) -> ResourceResult:
        """Issue one HTTP request to the project. Never raises; always closes the client."""
        import httpx

        from velune.resources.sql_base import sanitize_text

        try:
            async with self._client(config, service_role=service_role) as client:
                resp = await client.request(method, path, **kw)
        except httpx.TimeoutException as exc:
            return ResourceResult.failure(
                f"Supabase timed out: {type(exc).__name__}",
                code=ErrorCode.TIMEOUT,
                hint="The project took too long to answer.",
            )
        except (httpx.HTTPError, OSError) as exc:
            return ResourceResult.failure(
                f"Supabase unreachable: {type(exc).__name__}: {sanitize_text(str(exc), 160)}",
                code=ErrorCode.CONNECTION_FAILED,
                hint="Is the project paused, or the URL wrong?",
            )
        if resp.status_code >= 400:
            message = ""
            try:
                body = resp.json()
                if isinstance(body, dict):
                    message = str(body.get("message") or "")
            except ValueError:
                message = resp.text
            code, hint = _http_error(resp.status_code, message)
            message = self._scrub_keys(config, message)
            return ResourceResult.failure(
                f"Supabase returned HTTP {resp.status_code}: {sanitize_text(message, 200)}",
                code=code,
                hint=hint,
            )
        try:
            data = resp.json()
        except ValueError:
            data = resp.text
        return ResourceResult.success(data=data)

    async def _verify(self, config: dict[str, Any]) -> ResourceResult:
        # PostgREST root returns the OpenAPI spec when the project + key are valid.
        result = await self._request(config, "GET", "/rest/v1/")
        if result.ok:
            result.action = "verify"
            result.data = {"reachable": True, "project": urlparse(config["_base_url"]).hostname}
        return result

    async def _do_verify(self, config: dict[str, Any], params: dict[str, Any]) -> ResourceResult:
        return await self._verify(config)

    @staticmethod
    def _definitions(spec: Any) -> dict[str, Any]:
        spec = spec if isinstance(spec, dict) else {}
        defs = spec.get("definitions") or (spec.get("components", {}) or {}).get("schemas", {})
        return defs if isinstance(defs, dict) else {}

    async def _do_list_tables(
        self, config: dict[str, Any], params: dict[str, Any]
    ) -> ResourceResult:
        result = await self._request(config, "GET", "/rest/v1/")
        if not result.ok:
            return result
        tables = sorted(self._definitions(result.data))
        return ResourceResult.success(
            "list_tables",
            {
                "tables": tables[:500],
                "truncated": len(tables) > 500,
                "connection": config.get("_profile"),
            },
        )

    async def _do_inspect_schema(
        self, config: dict[str, Any], params: dict[str, Any]
    ) -> ResourceResult:
        table = params.get("table")
        if not table or not _IDENTIFIER.match(str(table)):
            return ResourceResult.failure(
                "inspect_schema requires a valid 'table' identifier.",
                action="inspect_schema",
                code=ErrorCode.VALIDATION_FAILED,
            )
        result = await self._request(config, "GET", "/rest/v1/")
        if not result.ok:
            return result
        table_def = self._definitions(result.data).get(str(table))
        if table_def is None:
            return ResourceResult.failure(
                f"Table '{table}' not found in schema.",
                action="inspect_schema",
                code=ErrorCode.NOT_FOUND,
                hint="Use list_tables; Row Level Security and grants decide what is exposed.",
            )
        return ResourceResult.success("inspect_schema", table_def)

    async def _do_storage_buckets(
        self, config: dict[str, Any], params: dict[str, Any]
    ) -> ResourceResult:
        service_role = bool(params.get("service_role"))
        result = await self._request(config, "GET", "/storage/v1/bucket", service_role=service_role)
        if result.ok:
            result.action = "storage_buckets"
        return result

    async def _do_query(self, config: dict[str, Any], params: dict[str, Any]) -> ResourceResult:
        table = params.get("table")
        if not table or not _IDENTIFIER.match(str(table)):
            return ResourceResult.failure(
                "query requires a valid 'table' identifier.",
                action="query",
                code=ErrorCode.VALIDATION_FAILED,
            )
        profile = params.get("_limits")
        cap = int(profile.limits.max_rows) if isinstance(profile, Profile) else 200
        try:
            limit = max(1, min(int(params.get("limit", 100)), cap))
            offset = max(0, int(params.get("offset", 0)))
        except (TypeError, ValueError):
            return ResourceResult.failure(
                "'limit' and 'offset' must be integers.",
                action="query",
                code=ErrorCode.VALIDATION_FAILED,
            )
        select = params.get("select", "*")
        if not _SELECT.match(str(select)):
            return ResourceResult.failure(
                "Invalid 'select' clause.", action="query", code=ErrorCode.VALIDATION_FAILED
            )
        query: dict[str, str] = {"select": str(select), "limit": str(limit)}
        if offset:
            query["offset"] = str(offset)
        order = params.get("order")
        if order:
            if not _IDENTIFIER.match(str(order)):
                return ResourceResult.failure(
                    "'order' must be a column identifier.",
                    action="query",
                    code=ErrorCode.VALIDATION_FAILED,
                )
            query["order"] = f"{order}.{'desc' if params.get('descending') else 'asc'}"
        column, op, value = (
            params.get("filter_column"),
            params.get("filter_op"),
            params.get("filter_value"),
        )
        if column or op or value is not None:
            if not (
                isinstance(column, str)
                and _IDENTIFIER.match(column)
                and op in _FILTER_OPS
                and isinstance(value, str)
                and len(value) <= 200
            ):
                return ResourceResult.failure(
                    "A filter needs filter_column, filter_op and filter_value.",
                    action="query",
                    code=ErrorCode.VALIDATION_FAILED,
                )
            query[column] = f"{op}.{value}"
        service_role = bool(params.get("service_role"))
        result = await self._request(
            config, "GET", f"/rest/v1/{table}", params=query, service_role=service_role
        )
        if not result.ok:
            return result
        return ResourceResult.success(
            "query", self._shape(result.data, limit, profile, service_role)
        )

    def _shape(self, data: Any, limit: int, profile: Any, service_role: bool) -> dict[str, Any]:
        """Bound, JSON-safe and column-redact a PostgREST row list."""
        from velune.resources.sql_results import ResultLimits

        limits = profile.limits if isinstance(profile, Profile) else ResultLimits()
        rows_in = data if isinstance(data, list) else [data]
        rows: list[dict[str, Any]] = []
        redacted: set[str] = set()
        budget = limits.max_chars
        truncated = len(rows_in) > limit
        for row in rows_in[:limit]:
            if not isinstance(row, dict):
                rows.append({"value": json_safe(row, limits)})
                continue
            clean: dict[str, Any] = {}
            for key, value in row.items():
                if column_is_sensitive(str(key), limits) and value is not None:
                    clean[str(key)] = MASK
                    redacted.add(str(key))
                else:
                    clean[str(key)] = json_safe(value, limits)
            cost = len(str(clean))
            if rows and cost > budget:
                truncated = True
                break
            budget -= cost
            rows.append(clean)
        out: dict[str, Any] = {
            "rows": rows,
            "rowcount": len(rows),
            "truncated": truncated or len(rows) >= limit,
            "role": "service_role" if service_role else "anon (Row Level Security applies)",
        }
        if redacted:
            out["redacted_columns"] = sorted(redacted)
        if out["truncated"]:
            out["note"] = "There may be more rows: use offset or a filter."
        return out

    async def _do_edge_functions(
        self, config: dict[str, Any], params: dict[str, Any]
    ) -> ResourceResult:
        # The public REST surface does not enumerate functions; that needs the
        # Management API. Report that honestly rather than failing opaquely.
        return ResourceResult.success(
            "edge_functions",
            {
                "note": "Edge Function enumeration requires the Supabase Management API "
                "(a personal access token), which is out of scope for the local "
                "project connector.",
                "functions": [],
            },
        )

    async def _do_rls_policies(
        self, config: dict[str, Any], params: dict[str, Any]
    ) -> ResourceResult:
        if not self._has_service_role(config):
            return ResourceResult.failure(
                "Inspecting RLS policies requires a service-role key, which is not configured.",
                action="rls_policies",
                code=ErrorCode.UNAVAILABLE,
            )
        # pg-meta exposes policies under the service role. Best-effort; a project
        # without pg-meta enabled returns a structured error rather than crashing.
        result = await self._request(config, "GET", "/pg/policies", service_role=True)
        if result.ok:
            result.action = "rls_policies"
        return result
