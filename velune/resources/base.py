"""Resource Connector Framework — core abstractions.

A *resource* is an external part of the user's local development environment
that Velune can inspect and (with permission) act on: a Docker daemon, a local
PostgreSQL/MySQL server, a Supabase project, and so on.

The framework mirrors the provider/tool subsystems:

- Every connector implements one interface (:class:`ResourceConnector`) so the
  :class:`~velune.resources.manager.ResourceManager` treats them uniformly.
- Every action a connector exposes is a declared :class:`ResourceCapability`
  carrying a :class:`ResourcePermission`. The manager gates non-read actions
  through the same approval philosophy as
  :class:`velune.orchestration.tool_loop.ToolLoopRunner` — read is auto-approved,
  everything else needs a yes.
- Connectors never raise across the ``execute``/``connect`` boundary for
  operational failures; they return a structured :class:`ResourceResult`. A CLI
  that surfaces "Docker is not installed" must never crash.

Adding Redis, Firebase, MongoDB, Kubernetes, etc. later means writing one new
:class:`ResourceConnector` subclass and registering it — no changes here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from velune._compat import StrEnum

if TYPE_CHECKING:
    from velune.permissions.actions import Action as PermAction
    from velune.resources.ownership import OwnershipLedger, ResourceRef


class ResourcePermission(StrEnum):
    """Permission tiers a resource action can require.

    Ordered least→most privileged. The manager auto-approves :attr:`READ` and
    requires explicit approval for the rest, matching the tool-loop's
    read-only-auto-approve policy.
    """

    READ = "read"  # inspect only; never mutates state — auto-approved
    WRITE = "write"  # mutate state (start/stop container, DDL) — approval required
    EXECUTE = "execute"  # run/build/compose — approval required
    ADMIN = "admin"  # destructive / privileged (DROP, service-role) — explicit confirm


class ResourceState(StrEnum):
    """Lifecycle state of a connector."""

    UNAVAILABLE = "unavailable"  # the backing software/service isn't present
    DISCONNECTED = "disconnected"  # available but no active session
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class Lifecycle(StrEnum):
    """How a connector's session is managed by the :class:`ResourceManager`.

    The manager owns connect / reuse / health / disconnect for every strategy;
    connectors only implement the application-specific open and close. Pooling
    belongs to strategies that need it — never forced on a stateless client.
    """

    STATELESS = "stateless"  # no session (a CLI wrapper, a one-shot HTTP client)
    PERSISTENT = "persistent"  # one long-lived client or connection, reused
    POOL = "pool"  # a driver-managed connection pool
    SESSION = "session"  # a protocol session owned elsewhere (MCP)
    SUPERVISED_PROCESS = "process"  # child processes Velune started and must clean up


class ErrorCode(StrEnum):
    """Normalized failure categories, stable across every connector.

    Both the model and the user see these; the message alongside explains the
    specifics without ever including credentials.
    """

    CONNECTION_FAILED = "connection_failed"
    AUTHENTICATION_FAILED = "authentication_failed"
    AUTHORIZATION_DENIED = "authorization_denied"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    TIMEOUT = "timeout"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    UNAVAILABLE = "application_unavailable"
    ADAPTER_FAILURE = "adapter_failure"
    VALIDATION_FAILED = "validation_failed"
    # Network-facing failures (the HTTP integration). Each names where a request
    # stopped, so the model can tell a firewall problem from a bad URL.
    INVALID_URL = "invalid_url"
    NETWORK_POLICY = "network_policy_blocked"
    DNS_FAILED = "dns_resolution_failed"
    TLS_FAILED = "tls_validation_failed"
    RESPONSE_TOO_LARGE = "response_too_large"
    REDIRECT_REJECTED = "redirect_rejected"
    INVALID_RESPONSE = "invalid_response"


@dataclass(slots=True, frozen=True)
class ResourceCapability:
    """One action a connector can perform.

    Args:
        action:      Stable action id passed to :meth:`ResourceConnector.execute`.
        permission:  Authorization tier the manager enforces before running it.
        description: Human-readable summary for ``/resource info``.
        destructive: True for irreversible operations (DROP TABLE, ``compose
                     down -v``). The approver is told so it can demand a stronger
                     confirmation.
        scope:       Integration permission scope (``docker.container.start``).
                     Empty means ``<resource_id>.<action>``.
        category:    Capability group used by discovery (``inspect``,
                     ``containers``, ``compose`` …). Empty derives ``inspect`` for
                     READ actions and ``change`` for the rest.
        params:      The action's input schema as a flat JSON-schema fragment
                     (``{"properties": {...}, "required": [...]}``). Empty means
                     the action takes no parameters. Model-visible: it is
                     validated and whitelisted before it is ever shown.
    """

    action: str
    permission: ResourcePermission
    description: str = ""
    destructive: bool = False
    scope: str = ""
    category: str = ""
    params: dict[str, Any] | None = field(default=None, compare=False, hash=False)


@dataclass(slots=True, frozen=True)
class ResourceStatus:
    """Point-in-time status of a connector. Never carries secrets."""

    resource_id: str
    display_name: str
    state: ResourceState
    detail: str = ""
    #: Free-form, secret-free metadata (version, endpoint host, db name, …).
    info: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def connected(self) -> bool:
        return self.state is ResourceState.CONNECTED


@dataclass(slots=True, frozen=True)
class DiscoveryHint:
    """A resource the environment appears to offer.

    Produced by connector detection and by project-file scanning. Purely
    advisory — it drives the "Detected … Would you like to connect?" prompt and
    never carries a password.
    """

    resource_id: str  # which connector can handle it ("docker", "postgres", …)
    display_name: str
    detail: str = ""  # e.g. "localhost:5432" or "docker-compose.yml"
    source: str = ""  # what revealed it: "daemon", ".env", "docker-compose.yml"
    #: Secret-free connection hints a connector can pre-fill (host/port/db).
    suggested: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ResourceResult:
    """Structured outcome of any connector operation.

    Connectors return this instead of raising for operational failures, so the
    CLI and the (future) AI tool bridge get a uniform, crash-proof shape.
    """

    ok: bool
    action: str = ""
    data: Any = None
    error: str | None = None
    #: Set when the action was rejected by the approval framework rather than by
    #: an operational failure, so callers can message it differently.
    denied: bool = False
    #: Normalized failure category (None on success). The manager fills in
    #: ADAPTER_FAILURE for a connector failure that did not set one.
    code: ErrorCode | None = None
    #: Which integration produced the result (set by the manager).
    integration: str = ""
    #: Identifiers of the application resources the call touched or created.
    resource_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    #: Short remediation advice for the user and the model ("start Docker").
    hint: str = ""

    @classmethod
    def success(cls, action: str = "", data: Any = None) -> ResourceResult:
        return cls(ok=True, action=action, data=data)

    @classmethod
    def failure(
        cls,
        error: str,
        *,
        action: str = "",
        code: ErrorCode | None = None,
        hint: str = "",
    ) -> ResourceResult:
        return cls(ok=False, action=action, error=error, code=code, hint=hint)

    @classmethod
    def denied_result(cls, action: str, reason: str) -> ResourceResult:
        return cls(
            ok=False,
            action=action,
            error=reason,
            denied=True,
            code=ErrorCode.AUTHORIZATION_DENIED,
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form (what the model and the audit trail see)."""
        out: dict[str, Any] = {
            "ok": self.ok,
            "integration": self.integration,
            "action": self.action,
        }
        if self.ok:
            out["data"] = self.data
        else:
            out["error"] = {
                "code": (self.code or ErrorCode.ADAPTER_FAILURE).value,
                "message": self.error or "",
            }
            if self.hint:
                out["error"]["hint"] = self.hint
            if self.denied:
                out["error"]["denied"] = True
        if self.resource_ids:
            out["resource_ids"] = list(self.resource_ids)
        if self.warnings:
            out["warnings"] = list(self.warnings)
        out["duration_ms"] = round(self.duration_ms, 1)
        return out


@dataclass(slots=True, frozen=True)
class AuthorizationRequest:
    """What the manager hands an approver before running a gated action."""

    resource_id: str
    display_name: str
    action: str
    permission: ResourcePermission
    destructive: bool
    #: Secret-free preview of the action's parameters (e.g. the SQL text).
    preview: str = ""


#: Async policy callback. Returns True to allow the action. Mirrors the
#: tool-loop's ``Approver`` in spirit: a denial is data, never an exception.
Approver = Callable[[AuthorizationRequest], Awaitable[bool]]


class ResourceConnectorError(Exception):
    """Base for connector-internal errors. Operational failures are returned as
    :class:`ResourceResult`, not raised — this is reserved for programming
    errors (e.g. an action with no declared capability)."""


class ResourceConnector(ABC):
    """Common interface every resource integration implements.

    Concrete connectors set :attr:`resource_id` / :attr:`display_name` as class
    attributes and implement the lifecycle + execution methods. Detection
    (:meth:`discover`) must be safe to call even when the backing software is
    absent — it reports availability, it does not assume it.
    """

    #: Stable slug ("docker", "postgres", "mysql", "supabase").
    resource_id: str = ""
    #: Human-readable label for tables and prompts.
    display_name: str = ""
    #: How the manager manages this connector's session.
    lifecycle: Lifecycle = Lifecycle.STATELESS
    #: One sentence for discovery ("what can I do with this?"). Never put
    #: credentials, hosts or paths in it: it is shown to the model.
    description: str = ""
    #: Lowercase words that suggest this integration for a task (matching is a
    #: deterministic whole-word test; it only *suggests*, it never exposes).
    keywords: tuple[str, ...] = ()

    def __init__(self, name: str | None = None) -> None:
        # ``name`` distinguishes multiple instances of the same connector type
        # (e.g. two Postgres servers). Defaults to the resource_id.
        self.name = name or self.resource_id

    # ── Lifecycle ────────────────────────────────────────────────────────────

    @abstractmethod
    async def connect(self) -> ResourceResult:
        """Establish a session. Idempotent; returns success if already connected."""

    @abstractmethod
    async def disconnect(self) -> ResourceResult:
        """Tear down any session/pool. Safe to call when not connected."""

    @abstractmethod
    def status(self) -> ResourceStatus:
        """Return current state without side effects or network calls."""

    @abstractmethod
    async def discover(self) -> list[DiscoveryHint]:
        """Detect whether this resource is present in the environment.

        Must never raise and never block indefinitely — return an empty list
        when the backing software/service is absent or unreachable.
        """

    @abstractmethod
    def capabilities(self) -> list[ResourceCapability]:
        """Declare every action this connector exposes and its permission tier."""

    @abstractmethod
    async def execute(self, action: str, params: dict[str, Any] | None = None) -> ResourceResult:
        """Run *action*. Returns a structured result; does not raise on failure.

        Authorization is enforced by the manager *before* this is called; a
        connector may still defensively verify it is connected.
        """

    # ── Shared helpers ───────────────────────────────────────────────────────

    def capability_for(self, action: str) -> ResourceCapability | None:
        """Look up the declared capability for *action* (or None if unknown)."""
        for cap in self.capabilities():
            if cap.action == action:
                return cap
        return None

    def authorization_preview(self, action: str, params: dict[str, Any] | None) -> str:
        """Build a secret-free preview string for the approval prompt.

        Overridden by connectors that carry sensitive params (a DB connector
        renders the SQL, never the password). The default renders nothing to
        stay safe by construction.
        """
        return ""

    def effective_permission(
        self, action: str, params: dict[str, Any] | None
    ) -> tuple[ResourcePermission, bool] | None:
        """Compute the *actual* permission tier for a specific call.

        Some actions carry variable risk in their parameters — the DB
        connectors' single ``query`` action can be a read-only ``SELECT`` or a
        destructive ``DROP``. Returning ``(permission, destructive)`` here lets
        the manager gate the concrete call instead of the action's static tier;
        returning ``None`` (the default) means "use the declared capability".
        The value may only *raise* the tier relative to the declaration — the
        manager never trusts it to weaken a WRITE action down to READ.
        """
        return None

    # ── Authorization description ────────────────────────────────────────────

    def scope_for(
        self, action: str, params: dict[str, Any] | None, permission: ResourcePermission
    ) -> str:
        """The permission scope for this specific call.

        Defaults to the capability's declared scope, else ``<resource_id>.<action>``.
        Connectors whose single action spans several scopes (SQL ``query`` can
        read or write) override this to name the concrete one.
        """
        cap = self.capability_for(action)
        if cap is not None and cap.scope:
            return cap.scope
        return f"{self.resource_id}.{action}"

    def describe_actions(
        self,
        action: str,
        params: dict[str, Any] | None,
        *,
        ledger: OwnershipLedger | None = None,
    ) -> list[PermAction]:
        """Describe *action* as permission-gate actions, before it runs.

        The default maps the effective permission tier (declared capability,
        escalated by :meth:`effective_permission`) to an ``INTEGRATION_*``
        action type and risk:

        ==========  =====================  ======
        tier        action type            risk
        ==========  =====================  ======
        READ        INTEGRATION_READ       low
        WRITE       INTEGRATION_WRITE      medium
        EXECUTE     INTEGRATION_EXECUTE    medium
        ADMIN       INTEGRATION_WRITE      high
        destructive INTEGRATION_DELETE     high
        ==========  =====================  ======

        Overrides may add context (ownership, targets) and may only *raise* the
        risk — :meth:`ResourceManager.execute` enforces that.
        """
        from velune.permissions.actions import Action as GateAction
        from velune.permissions.actions import ActionType, Risk

        cap = self.capability_for(action)
        if cap is None:
            return []
        permission, destructive = resolve_effective(self, cap, action, params)
        scope = self.scope_for(action, params, permission)
        if destructive:
            action_type, risk = ActionType.INTEGRATION_DELETE, Risk.HIGH
        elif permission is ResourcePermission.READ:
            action_type, risk = ActionType.INTEGRATION_READ, Risk.LOW
        elif permission is ResourcePermission.EXECUTE:
            action_type, risk = ActionType.INTEGRATION_EXECUTE, Risk.MEDIUM
        elif permission is ResourcePermission.ADMIN:
            action_type, risk = ActionType.INTEGRATION_WRITE, Risk.HIGH
        else:
            action_type, risk = ActionType.INTEGRATION_WRITE, Risk.MEDIUM
        return [
            GateAction(
                action_type,
                f"{self.name}:{action}",
                cap.description or action,
                risk,
                detail=_safe_preview(self, action, params),
                scope=scope,
            )
        ]

    def ownership_effects(
        self, action: str, params: dict[str, Any] | None, result: ResourceResult
    ) -> list[tuple[ResourceRef, bool]]:
        """Resources a *successful* call created (True) or removed (False).

        The manager records these in the session's ownership ledger. Default:
        none — connectors that create resources override this.
        """
        return []

    async def health(self) -> bool:
        """Cheap liveness check of the current session. Default: status only."""
        return self.status().connected

    # ── Discovery support ────────────────────────────────────────────────────

    def configuration_problem(self) -> str | None:
        """Why this connector cannot be used until the user configures it, or None.

        Must not perform I/O beyond reading stored configuration, and the text
        must never contain a credential, host or path (it is shown to the model).
        """
        return None

    async def probe_availability(self) -> tuple[bool, str]:
        """Is the backing application present right now? ``(available, reason)``.

        Default: the application is present when :meth:`discover` finds it.
        Connectors that can tell "installed but stopped" from "absent" override
        this. Bounded by the caller; must never raise.
        """
        try:
            hints = await self.discover()
        except Exception:
            return False, "could not be probed"
        if hints:
            return True, ""
        return False, "not detected"


def resolve_effective(
    connector: ResourceConnector,
    cap: ResourceCapability,
    action: str,
    params: dict[str, Any] | None,
) -> tuple[ResourcePermission, bool]:
    """Declared tier, raised (never lowered) by the connector's per-call override."""
    permission, destructive = cap.permission, cap.destructive
    override = connector.effective_permission(action, params)
    if override is not None:
        ov_perm, ov_destructive = override
        if PERMISSION_RANK[ov_perm] >= PERMISSION_RANK[permission]:
            permission = ov_perm
        destructive = destructive or ov_destructive
    return permission, destructive


def _safe_preview(connector: ResourceConnector, action: str, params: dict[str, Any] | None) -> str:
    try:
        preview = connector.authorization_preview(action, params) or ""
    except Exception:
        return ""
    from velune.core.redaction import redact_secrets

    return redact_secrets(preview)[:300]


#: Tier ordering for "stronger wins" when resolving an escalated permission.
PERMISSION_RANK: dict[ResourcePermission, int] = {
    ResourcePermission.READ: 0,
    ResourcePermission.WRITE: 1,
    ResourcePermission.EXECUTE: 1,
    ResourcePermission.ADMIN: 2,
}
