"""ResourceManager — the registry + lifecycle + authorization hub for connectors.

Analogous to :class:`velune.providers.registry.ProviderRegistry` and
:class:`velune.tools.base.registry.ToolRegistry`: it owns a set of connectors,
loads their configuration, drives discovery, manages connect/disconnect, and —
critically — is the single choke point where authorization is enforced before
any non-read action runs.

It is designed to be handed to the (future) AI tool bridge as a service: an
`execute_resource_action` tool would call :meth:`execute` and inherit the exact
same approval gating the REPL uses, because the gate lives here, not in the UI.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from velune.resources.base import (
    Approver,
    AuthorizationRequest,
    DiscoveryHint,
    ResourceConnector,
    ResourcePermission,
    ResourceResult,
    ResourceState,
    ResourceStatus,
)
from velune.resources.discovery import scan_workspace

logger = logging.getLogger("velune.resources.manager")

#: Factory that builds a connector on demand (mirrors ProviderRegistry factories).
ConnectorFactory = Callable[[], ResourceConnector]

# Tier ordering for "stronger wins" when resolving an escalated permission.
_PERM_RANK: dict[ResourcePermission, int] = {
    ResourcePermission.READ: 0,
    ResourcePermission.WRITE: 1,
    ResourcePermission.EXECUTE: 1,
    ResourcePermission.ADMIN: 2,
}


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
    ) -> None:
        """
        Args:
            config:    The ``resources`` config subtree (per-connector
                       ``enabled`` / ``auto_connect`` flags). Missing → all
                       connectors enabled, none auto-connecting.
            approver:  Authorization policy for non-read actions. Defaults to
                       :func:`approve_read_only` (fail-closed).
            workspace: Project root used by file-based auto-discovery.
        """
        self._factories: dict[str, ConnectorFactory] = {}
        # Instantiated connectors, keyed by resource_id. Lazily built from
        # factories so importing the manager never imports a db driver.
        self._connectors: dict[str, ResourceConnector] = {}
        self._config = config or {}
        self._approver: Approver = approver or approve_read_only
        self._workspace = workspace

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
        """Install the authorization policy used to gate non-read actions."""
        self._approver = approver

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
            return ResourceResult.failure(
                f"No connector '{resource_id}' (unknown or disabled).", action="connect"
            )
        try:
            return await connector.connect()
        except Exception as exc:
            logger.debug("connect(%s) raised: %s", resource_id, exc, exc_info=True)
            return ResourceResult.failure(f"{type(exc).__name__}: {exc}", action="connect")

    async def disconnect(self, resource_id: str) -> ResourceResult:
        """Disconnect the named resource. Structured result — never raises."""
        connector = self.get(resource_id)
        if connector is None:
            return ResourceResult.failure(f"No connector '{resource_id}'.", action="disconnect")
        try:
            return await connector.disconnect()
        except Exception as exc:
            logger.debug("disconnect(%s) raised: %s", resource_id, exc, exc_info=True)
            return ResourceResult.failure(f"{type(exc).__name__}: {exc}", action="disconnect")

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
    ) -> ResourceResult:
        """Run *action* on *resource_id*, enforcing authorization first.

        The permission tier comes from the connector's declared capability for
        the action. READ runs immediately; WRITE/EXECUTE/ADMIN must be approved
        by *approver* (or the manager's default). A denial returns a structured
        ``denied`` result — it is never raised — so the caller (and the model,
        via the tool bridge) can adapt.
        """
        connector = self.get(resource_id)
        if connector is None:
            return ResourceResult.failure(
                f"No connector '{resource_id}' (unknown or disabled).", action=action
            )

        cap = connector.capability_for(action)
        if cap is None:
            available = ", ".join(sorted(c.action for c in connector.capabilities()))
            return ResourceResult.failure(
                f"Unknown action '{action}' for {resource_id}. Available: {available}",
                action=action,
            )

        # Resolve the *effective* tier: a connector may escalate a call above its
        # action's declared tier based on the parameters (e.g. a DROP passed to a
        # read-only ``query``). It can only raise, never weaken — we take the
        # stronger of the declared and computed tiers.
        permission, destructive = cap.permission, cap.destructive
        override = connector.effective_permission(action, params)
        if override is not None:
            ov_perm, ov_destructive = override
            if _PERM_RANK[ov_perm] >= _PERM_RANK[permission]:
                permission = ov_perm
            destructive = destructive or ov_destructive

        if permission is not ResourcePermission.READ:
            request = AuthorizationRequest(
                resource_id=resource_id,
                display_name=connector.display_name,
                action=action,
                permission=permission,
                destructive=destructive,
                preview=connector.authorization_preview(action, params),
            )
            approve = approver or self._approver
            try:
                allowed = await approve(request)
            except Exception as exc:  # an approver crash must fail closed
                logger.warning("Approver raised for %s.%s; denying: %s", resource_id, action, exc)
                allowed = False
            if not allowed:
                return ResourceResult.denied_result(
                    action, f"Permission denied for {cap.permission.value} action '{action}'."
                )

        try:
            return await connector.execute(action, params)
        except Exception as exc:
            logger.debug("execute(%s.%s) raised: %s", resource_id, action, exc, exc_info=True)
            return ResourceResult.failure(f"{type(exc).__name__}: {exc}", action=action)


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
) -> ResourceManager:
    """Construct a ResourceManager with all built-in connectors registered."""
    manager = ResourceManager(config=config, workspace=workspace, approver=approver)
    manager.register_defaults()
    return manager
