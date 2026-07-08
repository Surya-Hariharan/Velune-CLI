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
from typing import Any

from velune._compat import StrEnum


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
    """

    action: str
    permission: ResourcePermission
    description: str = ""
    destructive: bool = False


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

    @classmethod
    def success(cls, action: str = "", data: Any = None) -> ResourceResult:
        return cls(ok=True, action=action, data=data)

    @classmethod
    def failure(cls, error: str, *, action: str = "") -> ResourceResult:
        return cls(ok=False, action=action, error=error)

    @classmethod
    def denied_result(cls, action: str, reason: str) -> ResourceResult:
        return cls(ok=False, action=action, error=reason, denied=True)


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
