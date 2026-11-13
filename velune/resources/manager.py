"""ResourceManager — the registry + lifecycle + authorization hub for connectors.

Analogous to :class:`velune.providers.registry.ProviderRegistry` and
:class:`velune.tools.base.registry.ToolRegistry`: it owns a set of connectors,
loads their configuration, drives discovery, manages connect/disconnect, and —
critically — is the single choke point where authorization is enforced before
any non-read action runs.

Authorization is *not* decided here. Every action runs as a
:class:`~velune.resources.bridge.ResourceOperationTool` through
:func:`velune.tools.base.tool.authorize_and_execute`, so the session's
:class:`~velune.permissions.gate.PermissionGate` applies the same PLAN / MANUAL /
AUTO policy, session grants, audit trail and hooks as every native tool. A
legacy :data:`Approver` callback (``ResourceManager(approver=...)``) is still
accepted: it becomes the gate's *ask* callback for a MANUAL-mode gate, so it is
consulted exactly when the policy says a human must decide — never instead of
the policy.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from velune.resources.base import (
    Approver,
    AuthorizationRequest,
    DiscoveryHint,
    ErrorCode,
    Lifecycle,
    ResourceConnector,
    ResourcePermission,
    ResourceResult,
    ResourceState,
    ResourceStatus,
    resolve_effective,
)
from velune.resources.discovery import scan_workspace
from velune.resources.ownership import OwnershipLedger

if TYPE_CHECKING:
    from velune.tools.base.tool import ToolCallContext

#: Builds the execution context (with the session's permission gate) for one
#: resource call. Returning None, or a context without a gate, fails closed for
#: every state-changing action.
ContextFactory = Callable[[], "ToolCallContext | None"]

logger = logging.getLogger("velune.resources.manager")

#: Factory that builds a connector on demand (mirrors ProviderRegistry factories).
ConnectorFactory = Callable[[], ResourceConnector]


async def approve_read_only(request: AuthorizationRequest) -> bool:
    """Default approver: allow READ actions, deny everything else.

    Fail-closed by construction — identical philosophy to the tool-loop's
    :func:`velune.orchestration.tool_loop.approve_readonly_only`. A REPL wires a
    richer interactive approver on top; absent one, no mutation ever runs.
    """
    return request.permission is ResourcePermission.READ


class ResourceManager:
    """Owns connector registration, lifecycle, discovery, and authorization."""

    def __init__(
        self,
        *,
        config: dict[str, Any] | None = None,
        approver: Approver | None = None,
        workspace: Any | None = None,
        context_factory: ContextFactory | None = None,
        ledger: OwnershipLedger | None = None,
    ) -> None:
        """
        Args:
            config:    The ``resources`` config subtree (per-connector
                       ``enabled`` / ``auto_connect`` flags). Missing → all
                       connectors enabled, none auto-connecting.
            approver:  Authorization policy for non-read actions. Defaults to
                       :func:`approve_read_only` (fail-closed).
            workspace: Project root used by file-based auto-discovery.
            context_factory: Supplies the session's execution context (with
                       its :class:`PermissionGate`). When set it takes
                       precedence over the default *approver*.
            ledger:    Ownership ledger for this session (created if absent).
        """
        self._factories: dict[str, ConnectorFactory] = {}
        # Instantiated connectors, keyed by resource_id. Lazily built from
        # factories so importing the manager never imports a db driver.
        self._connectors: dict[str, ResourceConnector] = {}
        self._config = config or {}
        self._approver: Approver = approver or approve_read_only
        self._workspace = workspace
        self._context_factory = context_factory
        self.ledger = ledger or OwnershipLedger()
        # One lock per resource so concurrent callers never open two sessions.
        self._locks: dict[str, asyncio.Lock] = {}

    # ── Registration ─────────────────────────────────────────────────────────

    def register(self, resource_id: str, factory: ConnectorFactory) -> None:
        """Register a connector *factory* under *resource_id*.

        Adding a new resource type is exactly this call plus the connector
        class — no changes to the manager, CLI, or config machinery are required
        beyond an optional config default.
        """
        self._factories[resource_id] = factory

    def register_defaults(self) -> None:
        """Register the built-in connectors (Docker, Postgres, MySQL, Supabase).

        Imports are deferred to call time so an unused DB driver is never loaded.
        """
        from velune.resources.docker.connector import DockerConnector
        from velune.resources.mysql.connector import MySQLConnector
        from velune.resources.postgres.connector import PostgresConnector
        from velune.resources.supabase.connector import SupabaseConnector

        self.register("docker", DockerConnector)
        self.register("postgres", PostgresConnector)
        self.register("mysql", MySQLConnector)
        self.register("supabase", SupabaseConnector)

    def set_approver(self, approver: Approver) -> None:
        """Install the legacy approver (the *ask* callback of a MANUAL gate)."""
        self._approver = approver

    def set_context_factory(self, factory: ContextFactory | None) -> None:
        """Route authorization through the session's permission gate."""
        self._context_factory = factory

    def _lock(self, resource_id: str) -> asyncio.Lock:
        lock = self._locks.get(resource_id)
        if lock is None:
            lock = self._locks[resource_id] = asyncio.Lock()
        return lock

    # ── Connector access ─────────────────────────────────────────────────────

    def _is_enabled(self, resource_id: str) -> bool:
        entry = self._config.get(resource_id) if isinstance(self._config, dict) else None
        if isinstance(entry, dict):
            return bool(entry.get("enabled", True))
        return True

    def get(self, resource_id: str) -> ResourceConnector | None:
        """Return the connector for *resource_id*, instantiating it on first use.

        Respects the ``enabled`` config flag: a disabled connector returns None.
        """
        if not self._is_enabled(resource_id):
            return None
        if resource_id in self._connectors:
            return self._connectors[resource_id]
        factory = self._factories.get(resource_id)
        if factory is None:
            return None
        try:
            connector = factory()
        except Exception as exc:  # a broken connector must not take down the manager
            logger.warning("Could not instantiate connector '%s': %s", resource_id, exc)
            return None
        self._connectors[resource_id] = connector
        return connector

    def list_ids(self) -> list[str]:
        """All registered connector ids that are enabled."""
        return [rid for rid in self._factories if self._is_enabled(rid)]

    # ── Discovery ────────────────────────────────────────────────────────────

    async def discover(self) -> list[DiscoveryHint]:
        """Aggregate live connector detection with project-file scanning.

        Runs every enabled connector's :meth:`discover` (each is crash-proof)
        and merges the workspace file scan. Deduplicated so a resource seen both
        live and in config appears once, preferring the live/detailed hint.
        """
        hints: list[DiscoveryHint] = []

        for rid in self.list_ids():
            connector = self.get(rid)
            if connector is None:
                continue
            try:
                hints.extend(await connector.discover())
            except Exception as exc:  # never let one connector abort discovery
                logger.debug("Connector '%s' discover() failed: %s", rid, exc)

        hints.extend(scan_workspace(self._workspace))

        # Dedupe by (resource_id, detail); keep the first (live) occurrence.
        seen: set[tuple[str, str]] = set()
        unique: list[DiscoveryHint] = []
        for hint in hints:
            key = (hint.resource_id, hint.detail)
            if key in seen:
                continue
            seen.add(key)
            unique.append(hint)
        return unique

    def auto_connect_targets(self) -> list[str]:
        """Connector ids whose config sets ``auto_connect: true`` and are enabled."""
        targets: list[str] = []
        if not isinstance(self._config, dict):
            return targets
        for rid in self.list_ids():
            entry = self._config.get(rid)
            if isinstance(entry, dict) and entry.get("auto_connect"):
                targets.append(rid)
        return targets

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def connect(self, resource_id: str) -> ResourceResult:
        """Connect the named resource. Structured result — never raises."""
        connector = self.get(resource_id)
        if connector is None:
            return _finish(
                ResourceResult.failure(
                    f"No connector '{resource_id}' (unknown or disabled).",
                    action="connect",
                    code=ErrorCode.UNAVAILABLE,
                ),
                resource_id,
            )
        return await self._connect(resource_id, connector, only_if_needed=False)

    async def _connect(
        self, resource_id: str, connector: ResourceConnector, *, only_if_needed: bool
    ) -> ResourceResult:
        # The state check and the connect happen under one per-resource lock,
        # so concurrent callers never open two sessions.
        async with self._lock(resource_id):
            if only_if_needed and _safe_state(connector) is ResourceState.CONNECTED:
                return _finish(ResourceResult.success(action="connect"), resource_id)
            try:
                result = await connector.connect()
            except Exception as exc:
                logger.debug("connect(%s) raised: %s", resource_id, exc, exc_info=True)
                result = ResourceResult.failure(
                    _exception_message(exc), action="connect", code=ErrorCode.CONNECTION_FAILED
                )
        if not result.ok and result.code is None:
            result.code = ErrorCode.CONNECTION_FAILED
        return _finish(result, resource_id)

    async def ensure_connected(self, resource_id: str) -> ResourceResult:
        """Connect only if not already connected (stateless connectors: no-op).

        Safe under concurrency: callers racing on the same resource share one
        connection attempt (the state is re-checked under a per-resource lock).
        """
        connector = self.get(resource_id)
        if connector is None:
            return _finish(
                ResourceResult.failure(
                    f"No connector '{resource_id}' (unknown or disabled).",
                    action="connect",
                    code=ErrorCode.UNAVAILABLE,
                ),
                resource_id,
            )
        if connector.lifecycle is Lifecycle.STATELESS:
            return _finish(ResourceResult.success(action="connect"), resource_id)
        return await self._connect(resource_id, connector, only_if_needed=True)

    async def health(self, resource_id: str) -> bool:
        """Liveness of an active session. Never raises."""
        connector = self.get(resource_id)
        if connector is None:
            return False
        try:
            return bool(await connector.health())
        except Exception as exc:
            logger.debug("health(%s) raised: %s", resource_id, exc)
            return False

    async def disconnect(self, resource_id: str) -> ResourceResult:
        """Disconnect the named resource. Structured result — never raises."""
        connector = self.get(resource_id)
        if connector is None:
            return _finish(
                ResourceResult.failure(
                    f"No connector '{resource_id}'.",
                    action="disconnect",
                    code=ErrorCode.UNAVAILABLE,
                ),
                resource_id,
            )
        async with self._lock(resource_id):
            try:
                result = await connector.disconnect()
            except Exception as exc:
                logger.debug("disconnect(%s) raised: %s", resource_id, exc, exc_info=True)
                result = ResourceResult.failure(
                    _exception_message(exc), action="disconnect", code=ErrorCode.ADAPTER_FAILURE
                )
        return _finish(result, resource_id)

    async def disconnect_all(self) -> None:
        """Best-effort teardown of every active session (called on REPL exit)."""
        for connector in list(self._connectors.values()):
            try:
                if connector.status().state is ResourceState.CONNECTED:
                    await connector.disconnect()
            except Exception as exc:
                logger.debug("disconnect_all: %s failed: %s", connector.resource_id, exc)

    # ── Status / capabilities ────────────────────────────────────────────────

    def status(self, resource_id: str) -> ResourceStatus | None:
        """Status for one connector, or None if unknown/disabled."""
        connector = self.get(resource_id)
        if connector is None:
            return None
        try:
            return connector.status()
        except Exception as exc:
            logger.debug("status(%s) raised: %s", resource_id, exc)
            return ResourceStatus(
                resource_id=resource_id,
                display_name=resource_id,
                state=ResourceState.ERROR,
                error=str(exc),
            )

    def all_status(self) -> list[ResourceStatus]:
        """Status for every enabled connector."""
        out: list[ResourceStatus] = []
        for rid in self.list_ids():
            st = self.status(rid)
            if st is not None:
                out.append(st)
        return out

    def capabilities(self, resource_id: str) -> list:
        """Declared capabilities for one connector (empty if unknown)."""
        connector = self.get(resource_id)
        if connector is None:
            return []
        try:
            return connector.capabilities()
        except Exception:
            return []

    def active_sessions(self) -> list[str]:
        """Resource ids with a live connection."""
        return [
            rid for rid, c in self._connectors.items() if _safe_state(c) is ResourceState.CONNECTED
        ]

    # ── Authorized execution ─────────────────────────────────────────────────

    async def execute(
        self,
        resource_id: str,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        approver: Approver | None = None,
        ctx: ToolCallContext | None = None,
    ) -> ResourceResult:
        """Run *action* on *resource_id* through the central authorization pipeline.

        The connector describes the call (scope, risk, ownership); the call then
        runs as a :class:`~velune.resources.bridge.ResourceOperationTool` via
        :func:`~velune.tools.base.tool.authorize_and_execute`, where the
        permission gate decides. The gate comes from, in order: *ctx*, a
        per-call *approver* (wrapped as a MANUAL-mode gate), the manager's
        context factory, or the manager's default approver (likewise wrapped).

        A denial, an unknown action, or a connector failure is returned as a
        structured, redacted :class:`ResourceResult` — never raised.
        """
        from velune.permissions.gate import ActionDeniedError
        from velune.resources.bridge import ResourceOperationTool
        from velune.tools.base.tool import (
            ToolBlockedError,
            ToolPermissionError,
            authorize_and_execute,
        )

        started = time.perf_counter()
        params = dict(params or {})
        connector = self.get(resource_id)
        if connector is None:
            return _finish(
                ResourceResult.failure(
                    f"No connector '{resource_id}' (unknown or disabled).",
                    action=action,
                    code=ErrorCode.UNAVAILABLE,
                ),
                resource_id,
                started,
            )

        cap = connector.capability_for(action)
        if cap is None:
            available = ", ".join(sorted(c.action for c in connector.capabilities()))
            return _finish(
                ResourceResult.failure(
                    f"Unknown action '{action}' for {resource_id}. Available: {available}",
                    action=action,
                    code=ErrorCode.UNSUPPORTED_OPERATION,
                ),
                resource_id,
                started,
            )

        tool = ResourceOperationTool(connector, action, ledger=self.ledger)
        call_ctx = self._resolve_context(connector, action, params, approver=approver, ctx=ctx)
        try:
            result = await authorize_and_execute(tool, call_ctx, params=params)
        except (ActionDeniedError, ToolPermissionError) as exc:
            return _finish(
                ResourceResult.denied_result(action, _denial_reason(exc)), resource_id, started
            )
        except ToolBlockedError as exc:
            return _finish(
                ResourceResult.denied_result(action, f"Blocked by a hook: {exc.reason}"),
                resource_id,
                started,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("execute(%s.%s) raised: %s", resource_id, action, exc, exc_info=True)
            return _finish(
                ResourceResult.failure(
                    _exception_message(exc), action=action, code=ErrorCode.ADAPTER_FAILURE
                ),
                resource_id,
                started,
            )

        if not isinstance(result, ResourceResult):
            result = ResourceResult.success(action=action, data=result)
        if result.ok:
            self._record_ownership(connector, action, params, result)
        return _finish(result, resource_id, started)

    def _resolve_context(
        self,
        connector: ResourceConnector,
        action: str,
        params: dict[str, Any],
        *,
        approver: Approver | None,
        ctx: ToolCallContext | None,
    ) -> ToolCallContext | None:
        if ctx is not None:
            return ctx
        if approver is None and self._context_factory is not None:
            try:
                return self._context_factory()
            except Exception as exc:  # no context → authorize_and_execute fails closed
                logger.warning("Resource context factory failed; refusing changes: %s", exc)
                return None
        return self._legacy_context(connector, action, params, approver or self._approver)

    def _legacy_context(
        self,
        connector: ResourceConnector,
        action: str,
        params: dict[str, Any],
        approver: Approver,
    ) -> ToolCallContext:
        """A MANUAL-mode gate whose *ask* callback is the legacy approver.

        The policy still decides first (READ allowed; blocked or unscoped
        denied); the approver is consulted only for what the policy would put
        to a human, and an approver crash denies (fail closed).
        """
        from velune.core.redaction import redact_secrets
        from velune.permissions.boundary import Boundary
        from velune.permissions.gate import Approval, PermissionGate
        from velune.permissions.policy import PolicyState
        from velune.tools.base.tool import ToolCallContext

        cap = connector.capability_for(action)
        if cap is not None:
            permission, destructive = resolve_effective(connector, cap, action, params)
        else:
            permission, destructive = ResourcePermission.ADMIN, True
        try:
            preview = connector.authorization_preview(action, params) or ""
        except Exception:
            preview = ""
        request = AuthorizationRequest(
            resource_id=connector.resource_id,
            display_name=connector.display_name,
            action=action,
            permission=permission,
            destructive=destructive,
            preview=redact_secrets(preview),
        )

        async def ask(tool_name: str, decision: Any) -> Approval:
            try:
                allowed = await approver(request)
            except Exception as exc:  # an approver crash must fail closed
                logger.warning(
                    "Approver raised for %s.%s; denying: %s", connector.resource_id, action, exc
                )
                return Approval.DENY
            return Approval.ALLOW_ONCE if allowed else Approval.DENY

        workspace = Path(self._workspace) if self._workspace else Path.cwd()
        gate = PermissionGate(state=PolicyState(), boundary=Boundary(workspace), ask=ask)
        return ToolCallContext(
            run_id="resource-manager",
            actor="resource-manager",
            workspace=workspace,
            gate=gate,
        )

    def _record_ownership(
        self,
        connector: ResourceConnector,
        action: str,
        params: dict[str, Any],
        result: ResourceResult,
    ) -> None:
        try:
            effects = connector.ownership_effects(action, params, result)
        except Exception as exc:
            logger.debug("ownership_effects(%s.%s) failed: %s", connector.resource_id, action, exc)
            return
        for ref, created in effects:
            if created:
                self.ledger.record_created(ref)
            else:
                self.ledger.release(ref)
            if ref.identifier not in result.resource_ids:
                result.resource_ids.append(ref.identifier)


def _denial_reason(exc: BaseException) -> str:
    reason = getattr(exc, "reason", None) or str(exc)
    return f"Permission denied: {reason}"


def _exception_message(exc: BaseException) -> str:
    """A redacted, bounded description of an unexpected connector exception."""
    from velune.core.redaction import redact_secrets

    return redact_secrets(f"{type(exc).__name__}: {exc}")[:500]


def _finish(
    result: ResourceResult, resource_id: str, started: float | None = None
) -> ResourceResult:
    """Normalize every result leaving the manager: code, integration, timing, redaction."""
    from velune.core.redaction import redact_secrets, redact_structure

    result.integration = result.integration or resource_id
    if started is not None:
        result.duration_ms = (time.perf_counter() - started) * 1000.0
    if not result.ok and result.code is None:
        result.code = ErrorCode.ADAPTER_FAILURE
    if result.error:
        result.error = redact_secrets(result.error)
    if result.hint:
        result.hint = redact_secrets(result.hint)
    if result.data is not None:
        result.data = redact_structure(result.data)
    result.warnings = [redact_secrets(w) for w in result.warnings]
    return result


def _safe_state(connector: ResourceConnector) -> ResourceState:
    try:
        return connector.status().state
    except Exception:
        return ResourceState.ERROR


def build_default_manager(
    *,
    config: dict[str, Any] | None = None,
    workspace: Any | None = None,
    approver: Approver | None = None,
    context_factory: ContextFactory | None = None,
    ledger: OwnershipLedger | None = None,
) -> ResourceManager:
    """Construct a ResourceManager with all built-in connectors registered."""
    manager = ResourceManager(
        config=config,
        workspace=workspace,
        approver=approver,
        context_factory=context_factory,
        ledger=ledger,
    )
    manager.register_defaults()
    return manager
