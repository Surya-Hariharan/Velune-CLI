"""Supabase Cloud project connector.

Talks to a Supabase project over its REST surface (PostgREST + Storage) using
``httpx``. Credentials — the anon key and, optionally, the service-role key —
come from the encrypted keystore. The service-role key bypasses Row-Level
Security, so any capability that would use it is treated as non-read and must be
confirmed: it is never exercised silently.

Security posture:
- Only the configured project URL is ever contacted (host is validated up front),
  so this cannot be turned into an SSRF primitive via a crafted parameter.
- Table/identifier parameters are validated before being placed in a path.
- Keys are read from the encrypted store and never logged or echoed.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

from velune.resources.base import (
    DiscoveryHint,
    ResourceCapability,
    ResourceConnector,
    ResourcePermission,
    ResourceResult,
    ResourceState,
    ResourceStatus,
)
from velune.resources.secrets import load_resource_secret

logger = logging.getLogger("velune.resources.supabase")

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")
_TIMEOUT = 10.0


class SupabaseConnector(ResourceConnector):
    """Connector for a Supabase Cloud project."""

    resource_id = "supabase"
    display_name = "Supabase"

    def __init__(self, name: str | None = None, config: dict[str, Any] | None = None) -> None:
        super().__init__(name)
        self._explicit_config = config
        self._state = ResourceState.DISCONNECTED
        self._error: str | None = None
        self._url: str | None = None
        self._info: dict[str, Any] = {}

    # ── Config ───────────────────────────────────────────────────────────────

    def _resolve_config(self) -> dict[str, Any] | None:
        if self._explicit_config:
            return self._explicit_config
        return load_resource_secret(self.resource_id, self.name)

    @staticmethod
    def _validate_url(url: str) -> str | None:
        """Return a normalized https project URL, or None if unacceptable."""
        try:
            parsed = urlparse(url)
        except ValueError:
            return None
        if parsed.scheme != "https" or not parsed.hostname:
            return None
        return f"https://{parsed.hostname}"

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
        return httpx.AsyncClient(base_url=self._url or "", headers=headers, timeout=_TIMEOUT)

    def _has_service_role(self, config: dict[str, Any]) -> bool:
        return bool(config.get("service_role_key") or config.get("service_key"))

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def connect(self) -> ResourceResult:
        if not self.driver_available():
            self._state = ResourceState.UNAVAILABLE
            self._error = "httpx is not installed; the Supabase connector is unavailable."
            return ResourceResult.failure(self._error, action="connect")

        config = self._resolve_config()
        if not config or not (config.get("url")):
            self._state = ResourceState.DISCONNECTED
            return ResourceResult.failure(
                f"No stored configuration for Supabase '{self.name}'. "
                "Configure a project URL and anon key first.",
                action="connect",
            )

        url = self._validate_url(str(config["url"]))
        if url is None:
            self._state = ResourceState.ERROR
            self._error = "Invalid Supabase URL — must be an https:// project URL."
            return ResourceResult.failure(self._error, action="connect")
        self._url = url

        self._state = ResourceState.CONNECTING
        verify = await self._verify(config)
        if not verify.ok:
            self._state = ResourceState.ERROR
            self._error = verify.error
            return verify

        self._state = ResourceState.CONNECTED
        self._error = None
        self._info = {
            "url": url,
            "service_role": self._has_service_role(config),
        }
        return ResourceResult.success(action="connect", data=dict(self._info))

    async def disconnect(self) -> ResourceResult:
        self._state = ResourceState.DISCONNECTED
        self._url = None
        return ResourceResult.success(action="disconnect")

    def status(self) -> ResourceStatus:
        return ResourceStatus(
            resource_id=self.resource_id,
            display_name=self.display_name,
            state=self._state,
            detail=(urlparse(self._url).hostname if self._url else ""),
            info=dict(self._info),
            error=self._error,
        )

    async def discover(self) -> list[DiscoveryHint]:
        """A stored/config'd Supabase project surfaces as a hint (no network)."""
        config = self._resolve_config()
        if config and config.get("url"):
            host = ""
            try:
                host = urlparse(str(config["url"])).hostname or ""
            except ValueError:
                host = ""
            return [
                DiscoveryHint(
                    resource_id=self.resource_id,
                    display_name="Supabase project",
                    detail=host or "configured",
                    source="config",
                )
            ]
        return []

    # ── Capabilities ─────────────────────────────────────────────────────────

    def capabilities(self) -> list[ResourceCapability]:
        read = ResourcePermission.READ
        write = ResourcePermission.WRITE
        return [
            ResourceCapability("verify", read, "Verify the project is reachable"),
            ResourceCapability("list_tables", read, "List tables exposed via PostgREST"),
            ResourceCapability("inspect_schema", read, "Inspect a table's columns"),
            ResourceCapability("storage_buckets", read, "List Storage buckets"),
            ResourceCapability("query", read, "Read rows from a table (read-only)"),
            ResourceCapability("edge_functions", read, "List Edge Function metadata"),
            # Uses the service-role key → privileged; must be confirmed.
            ResourceCapability(
                "rls_policies", write, "Inspect RLS policies (requires service role)"
            ),
        ]

    def effective_permission(
        self, action: str, params: dict[str, Any] | None
    ) -> tuple[ResourcePermission, bool] | None:
        """Escalate any call that would use the service-role key.

        The service-role key bypasses RLS, so exercising it is a privileged act
        that must be confirmed regardless of which action requested it.
        """
        params = params or {}
        if params.get("service_role"):
            return (ResourcePermission.WRITE, False)
        return None

    def authorization_preview(self, action: str, params: dict[str, Any] | None) -> str:
        params = params or {}
        target = params.get("table") or ""
        role = "service-role" if params.get("service_role") else "anon"
        return f"supabase {action} {target} [{role}]".strip()

    # ── Execution ────────────────────────────────────────────────────────────

    async def execute(self, action: str, params: dict[str, Any] | None = None) -> ResourceResult:
        params = params or {}
        if not self.driver_available():
            return ResourceResult.failure("httpx is not installed.", action=action)
        config = self._resolve_config()
        if not config or not config.get("url"):
            return ResourceResult.failure("Supabase is not configured.", action=action)
        if self._url is None:
            self._url = self._validate_url(str(config["url"]))
            if self._url is None:
                return ResourceResult.failure("Invalid Supabase URL.", action=action)

        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            return ResourceResult.failure(f"Unsupported action '{action}'.", action=action)
        return await handler(config, params)

    async def _request(
        self,
        config: dict[str, Any],
        method: str,
        path: str,
        *,
        service_role: bool = False,
        **kw: Any,
    ) -> ResourceResult:
        """Issue one HTTP request to the project. Never raises."""
        import httpx

        try:
            async with self._client(config, service_role=service_role) as client:
                resp = await client.request(method, path, **kw)
        except (httpx.HTTPError, OSError) as exc:
            return ResourceResult.failure(f"Supabase unreachable: {type(exc).__name__}: {exc}")
        if resp.status_code >= 400:
            return ResourceResult.failure(
                f"Supabase returned HTTP {resp.status_code}: {resp.text[:200]}"
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
        return result

    async def _do_verify(self, config: dict[str, Any], params: dict[str, Any]) -> ResourceResult:
        return await self._verify(config)

    async def _do_list_tables(
        self, config: dict[str, Any], params: dict[str, Any]
    ) -> ResourceResult:
        result = await self._request(config, "GET", "/rest/v1/")
        if not result.ok:
            return result
        spec = result.data if isinstance(result.data, dict) else {}
        definitions = spec.get("definitions") or (spec.get("components", {}) or {}).get(
            "schemas", {}
        )
        tables = sorted(definitions.keys()) if isinstance(definitions, dict) else []
        return ResourceResult.success(action="list_tables", data={"tables": tables})

    async def _do_inspect_schema(
        self, config: dict[str, Any], params: dict[str, Any]
    ) -> ResourceResult:
        table = params.get("table")
        if not table or not _IDENTIFIER.match(str(table)):
            return ResourceResult.failure(
                "inspect_schema requires a valid 'table' identifier.", action="inspect_schema"
            )
        result = await self._request(config, "GET", "/rest/v1/")
        if not result.ok:
            return result
        spec = result.data if isinstance(result.data, dict) else {}
        definitions = spec.get("definitions") or (spec.get("components", {}) or {}).get(
            "schemas", {}
        )
        table_def = (definitions or {}).get(str(table))
        if table_def is None:
            return ResourceResult.failure(
                f"Table '{table}' not found in schema.", action="inspect_schema"
            )
        return ResourceResult.success(action="inspect_schema", data=table_def)

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
                "query requires a valid 'table' identifier.", action="query"
            )
        limit = int(params.get("limit", 100))
        select = params.get("select", "*")
        if not re.match(r"^[A-Za-z0-9_,*() ]+$", str(select)):
            return ResourceResult.failure("Invalid 'select' clause.", action="query")
        result = await self._request(
            config, "GET", f"/rest/v1/{table}", params={"select": select, "limit": str(limit)}
        )
        if result.ok:
            result.action = "query"
        return result

    async def _do_edge_functions(
        self, config: dict[str, Any], params: dict[str, Any]
    ) -> ResourceResult:
        # The public REST surface does not enumerate functions; that needs the
        # Management API. Report that honestly rather than failing opaquely.
        return ResourceResult.success(
            action="edge_functions",
            data={
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
            )
        # pg-meta exposes policies under the service role. Best-effort; a project
        # without pg-meta enabled returns a structured error rather than crashing.
        result = await self._request(config, "GET", "/pg/policies", service_role=True)
        if result.ok:
            result.action = "rls_policies"
        return result
