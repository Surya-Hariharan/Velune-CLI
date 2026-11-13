"""Model-facing integration tools: discovery, per-operation tools and the dispatcher.

This is how the agent *sees* integrations. It adds no execution path and no
permission of its own:

* The three **discovery tools** (``integrations_list`` / ``_enable`` / ``_disable``)
  only read catalog state and change which tools the model is shown. They are
  READ actions in the permission policy, so they work in every mode — and they
  grant nothing. Enabling an integration makes its operations *visible*; each
  call still goes through ``authorize_and_execute`` and the permission gate.
* A **per-operation tool** (``docker__stop``) is a :class:`ResourceOperationTool` —
  the same bridge A1 introduced — with a flat, provider-portable schema, strict
  argument validation and a JSON result. The connector describes the call, the
  gate decides, then the manager's own post-processing (ownership, redaction,
  normalization) produces the result.
* The **dispatcher** (``integration_call``) is a controlled fallback for when more
  operations are enabled than fit the tool budget. It is authorized as the
  *target* operation, never as itself (see ``resolve_call``), so the gate sees the
  real scope and a session "always allow" is as precise as for a direct tool.

An operation that is not enabled has no route, so a hand-built call to it is an
"unknown tool" error; the dispatcher refuses anything outside the enabled set.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from velune.permissions.actions import Action, ActionType, Risk
from velune.resources.base import ErrorCode, ResourceConnector
from velune.resources.bridge import ResourceOperationTool
from velune.resources.capabilities import (
    DEFAULT_MAX_TOOLS,
    CapabilityCatalog,
    CapabilityScope,
    OperationManifest,
    compact_payload,
    validate_arguments,
)
from velune.tools.base.tool import BaseTool, ToolPermission

if TYPE_CHECKING:
    from velune.resources.base import DiscoveryHint


def _access(op: OperationManifest) -> str:
    if op.destructive:
        return "destructive"
    return "read" if op.read_only else "change"


# ── Per-operation tool ──────────────────────────────────────────────────────


class ModelOperationTool(ResourceOperationTool):
    """One connector operation as the model calls it: flat arguments, JSON result."""

    def __init__(
        self,
        manager: Any,
        connector: ResourceConnector,
        op: OperationManifest,
        *,
        on_unavailable: Any = None,
        on_success: Any = None,
    ) -> None:
        super().__init__(connector, op.action, ledger=manager.ledger)
        self._manager = manager
        self._op = op
        self._on_unavailable = on_unavailable
        self._on_success = on_success

    @property
    def operation(self) -> OperationManifest:
        return self._op

    def get_name(self) -> str:
        return self._op.tool_name

    def get_description(self) -> str:
        suffix = "" if self._op.read_only else " Changes state; subject to approval."
        return f"{self._op.description}.{suffix}".replace("..", ".")

    def get_schema(self) -> dict[str, Any]:
        return self._op.schema

    def get_required_permissions(self) -> set[ToolPermission]:
        return {
            ToolPermission.INTEGRATION_READ
            if self._op.read_only
            else ToolPermission.INTEGRATION_WRITE
        }

    def validate_input(self, payload: dict[str, Any]) -> None:
        validate_arguments(self._op.schema, self._op.constraints, payload)

    def describe_actions(self, args: dict[str, Any], boundary: Any) -> list[Any]:
        if not self._still_matches():
            # The connector no longer declares what was exposed: refuse (fail closed).
            return [
                Action(
                    ActionType.INTEGRATION_EXECUTE,
                    f"{self._op.integration}:{self._op.action}",
                    "the integration changed since it was enabled",
                    Risk.HIGH,
                )
            ]
        return super().describe_actions({"params": dict(args)}, boundary)

    def _still_matches(self) -> bool:
        """The connector still declares this operation with the exposed scope and tier."""
        try:
            cap = self._connector.capability_for(self._op.action)
            if cap is None:
                return False
            scope = self._connector.scope_for(self._op.action, None, cap.permission)
            return (
                scope == self._op.scope
                and cap.permission.value == self._op.permission
                and bool(cap.destructive) == self._op.destructive
            )
        except Exception:
            return False

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        result = await self._manager.run_authorized(self._connector, self._op.action, kwargs)
        if result.ok and self._on_success is not None:
            self._on_success(self._op.integration)
        if not result.ok and result.code in (
            ErrorCode.UNAVAILABLE,
            ErrorCode.CONNECTION_FAILED,
        ):
            if self._on_unavailable is not None:
                self._on_unavailable(self._op.integration, result.error or "became unavailable")
        return compact_payload(result.to_dict())


# ── Discovery tools ─────────────────────────────────────────────────────────


class _DiscoveryTool(BaseTool):
    """Shared behaviour: read-only in the policy; changes only what the model is shown."""

    def __init__(self, source: IntegrationToolSource) -> None:
        self._source = source

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.INTEGRATION_READ}

    def describe_actions(self, args: dict[str, Any], boundary: Any) -> list[Any]:
        return [
            Action(
                ActionType.READ,
                f"integrations:{self.get_name()}",
                "discovery only — grants no permission",
            )
        ]


class IntegrationsListTool(_DiscoveryTool):
    def get_name(self) -> str:
        return "integrations_list"

    def get_description(self) -> str:
        return (
            "List the application integrations available on this machine (Docker, databases, "
            "background jobs) with their state and what they can do. Discovery only: it grants "
            "no permission. Use integrations_enable to get an integration's tools."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Only integrations matching this text."},
                "refresh": {
                    "type": "boolean",
                    "description": "Re-check availability instead of using a recent result.",
                },
            },
        }

    def validate_input(self, payload: dict[str, Any]) -> None:
        validate_arguments(self.get_schema(), {}, payload)

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        return await self._source.describe(
            query=kwargs.get("query"), refresh=bool(kwargs.get("refresh", False))
        )


class IntegrationsEnableTool(_DiscoveryTool):
    def get_name(self) -> str:
        return "integrations_enable"

    def get_description(self) -> str:
        ids = ", ".join(self._source.catalog_ids()) or "none"
        suggested = self._source.suggested_ids()
        hint = f" Suggested for this task: {', '.join(suggested)}." if suggested else ""
        return (
            "Make an integration's tools available to you, starting with your next turn. "
            f"Integrations: {ids}.{hint} Enabling grants no permission: every call is still "
            "checked and may need approval."
        )

    def get_schema(self) -> dict[str, Any]:
        ids = self._source.catalog_ids()
        integration: dict[str, Any] = {"type": "string", "description": "Integration id."}
        if ids:
            integration["enum"] = ids
        return {
            "type": "object",
            "properties": {
                "integration": integration,
                "categories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Only these capability categories (default: all).",
                },
            },
            "required": ["integration"],
        }

    def validate_input(self, payload: dict[str, Any]) -> None:
        # Not validated against the enum: an unknown id must reach execute() so the
        # model gets a structured "not found" it can act on, and fail closed there.
        schema = self.get_schema()
        schema["properties"]["integration"].pop("enum", None)
        validate_arguments(schema, {}, payload)

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        return await self._source.enable(kwargs["integration"], kwargs.get("categories"))


class IntegrationsDisableTool(_DiscoveryTool):
    def get_name(self) -> str:
        return "integrations_disable"

    def get_description(self) -> str:
        return "Stop offering an integration's tools to you (frees tool slots)."

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "integration": {"type": "string", "description": "An enabled integration id."}
            },
            "required": ["integration"],
        }

    def validate_input(self, payload: dict[str, Any]) -> None:
        validate_arguments(self.get_schema(), {}, payload)

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        return self._source.disable(kwargs["integration"])


# ── Dispatcher ──────────────────────────────────────────────────────────────


class IntegrationCallTool(BaseTool):
    """Call an enabled operation that did not fit the direct tool budget.

    Authorized as the target operation: the loop calls :meth:`resolve_call` and
    runs *that* tool (with its own name, scope and permissions) through the gate.
    """

    def __init__(self, source: IntegrationToolSource) -> None:
        self._source = source

    def get_name(self) -> str:
        return "integration_call"

    def get_description(self) -> str:
        return (
            "Call an enabled integration operation that has no tool of its own (see the "
            "'via_integration_call' list from integrations_enable). Authorized exactly like "
            "the operation itself."
        )

    def get_schema(self) -> dict[str, Any]:
        ids = sorted({op.integration for op in self._source.scope.dispatched})
        integration: dict[str, Any] = {"type": "string", "description": "Integration id."}
        if ids:
            integration["enum"] = ids
        return {
            "type": "object",
            "properties": {
                "integration": integration,
                "operation": {"type": "string", "description": "Operation name."},
                "arguments": {
                    "type": "string",
                    "description": 'The operation\'s arguments as a JSON object, e.g. {"table": "users"}.',
                },
            },
            "required": ["integration", "operation"],
        }

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.INTEGRATION_WRITE}  # conservative until resolved

    def validate_input(self, payload: dict[str, Any]) -> None:
        self.resolve_call(payload)  # fail early with a precise reason

    def resolve_call(self, arguments: dict[str, Any]) -> tuple[BaseTool, dict[str, Any]]:
        """The operation tool and flat arguments this call stands for (or raise)."""
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be a JSON object")
        integration, operation = arguments.get("integration"), arguments.get("operation")
        if not isinstance(integration, str) or not isinstance(operation, str):
            raise ValueError("'integration' and 'operation' must be strings")
        raw = arguments.get("arguments", {})
        if isinstance(raw, str):
            try:
                raw = json.loads(raw) if raw.strip() else {}
            except ValueError as exc:
                raise ValueError("'arguments' must be a JSON object") from exc
        if not isinstance(raw, dict):
            raise ValueError("'arguments' must be a JSON object")
        op = self._source.scope.operation(integration, operation)
        if op is None:
            raise LookupError(
                f"Operation '{integration}.{operation}' is not enabled. "
                "Call integrations_enable first."
            )
        tool = self._source.operation_tool(op)
        if tool is None:
            raise LookupError(f"Integration '{integration}' is no longer available.")
        tool.validate_input(raw)
        return tool, raw

    def describe_actions(self, args: dict[str, Any], boundary: Any) -> list[Any]:
        try:
            tool, flat = self.resolve_call(args)
            return tool.describe_actions(flat, boundary)
        except Exception:
            # Unresolvable: present an unscoped action, which the policy refuses.
            return [
                Action(
                    ActionType.INTEGRATION_EXECUTE,
                    "integration_call",
                    "operation could not be resolved",
                    Risk.HIGH,
                )
            ]

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        tool, flat = self.resolve_call(kwargs)
        return await tool.execute(**flat)


# ── The source the tool loop pulls from ─────────────────────────────────────


class IntegrationToolSource:
    """Everything the tool loop needs to expose integrations to the model.

    Implements the loop's ``DynamicToolSource`` protocol: a ``version`` that
    changes when the model should be shown different tools, ``tools()`` for the
    current set, and a hint for calls to tools that are not enabled yet. The set
    is *rebuilt between model turns*, never mutated inside one.
    """

    def __init__(self, manager: Any, *, max_tools: int = DEFAULT_MAX_TOOLS) -> None:
        self.manager = manager
        self.catalog = CapabilityCatalog(manager)
        self.scope = CapabilityScope(max_tools)
        self._task_text = ""
        self._hints: list[DiscoveryHint] = []
        self._signature: tuple[Any, ...] = ()
        self._epoch = 0

    # -- run lifecycle --

    def begin_run(self, task_text: str, hints: list[DiscoveryHint] | None = None) -> None:
        """Start of a model run: remember the task and drop anything stale."""
        self._task_text = task_text or ""
        self._hints = list(hints or [])
        self.catalog.invalidate()
        self.scope.refresh(self.catalog)

    # -- DynamicToolSource --

    @property
    def version(self) -> int:
        signature = (tuple(self.catalog_ids()), tuple(sorted(self.catalog.problems())))
        if signature != self._signature:
            self._signature = signature
            self._epoch += 1
        return self.scope.version + 1_000_000 * self._epoch

    def tools(self) -> list[BaseTool]:
        tools: list[BaseTool] = [IntegrationsListTool(self), IntegrationsEnableTool(self)]
        if self.scope.enabled_ids():
            tools.append(IntegrationsDisableTool(self))
        for op in self.scope.exposed:
            tool = self.operation_tool(op)
            if tool is not None:
                tools.append(tool)
        if self.scope.dispatched:
            tools.append(IntegrationCallTool(self))
        return tools

    def unknown_tool_hint(self, name: str) -> str | None:
        integration, sep, action = name.partition("__")
        if not sep:
            return None
        manifest = self.catalog.manifest(integration)
        if manifest is None or manifest.operation(action) is None:
            return None
        return (
            f" '{name}' is not enabled yet: call integrations_enable with integration "
            f"'{integration}' first; its tools appear on your next turn."
        )

    # -- helpers shared by the tools --

    def catalog_ids(self) -> list[str]:
        return [m.id for m in self.catalog.manifests()]

    def suggested_ids(self) -> list[str]:
        return self.catalog.suggest(self._task_text, self._hints)

    def operation_tool(self, op: OperationManifest) -> ModelOperationTool | None:
        connector = self.manager.get(op.integration)
        if connector is None:
            return None
        return ModelOperationTool(
            self.manager,
            connector,
            op,
            on_unavailable=self.catalog.note_failure,
            on_success=self.catalog.note_success,
        )

    async def describe(self, *, query: str | None = None, refresh: bool = False) -> dict[str, Any]:
        rows = await self.catalog.describe(
            enabled=self.scope.enabled_ids(),
            query=query,
            task_text=self._task_text,
            hints=self._hints,
            refresh=refresh,
        )
        return {
            "integrations": rows,
            "tool_budget": {
                "limit": self.scope.max_tools,
                "exposed": len(self.scope.exposed) + (1 if self.scope.dispatched else 0),
            },
        }

    async def enable(self, integration: Any, categories: Any = None) -> dict[str, Any]:
        ids = self.catalog_ids()
        if not isinstance(integration, str) or integration not in ids:
            return {
                "enabled": False,
                "error": {
                    "code": ErrorCode.NOT_FOUND.value,
                    "message": f"Unknown or disabled integration. Available: {', '.join(ids) or 'none'}.",
                },
            }
        availability = await self.catalog.availability(integration)
        if not availability.usable:
            return {
                "enabled": False,
                "integration": integration,
                "state": availability.state,
                "reason": availability.reason,
            }
        manifest = self.catalog.manifest(integration)
        assert manifest is not None  # catalog_ids() only lists valid manifests
        cats = (
            [c for c in categories if isinstance(c, str)] if isinstance(categories, list) else None
        )
        outcome = self.scope.enable(manifest, cats)
        if not outcome.exposed and not outcome.dispatched:
            return {
                "enabled": False,
                "integration": integration,
                "error": {
                    "code": ErrorCode.VALIDATION_FAILED.value,
                    "message": f"No such category. Categories: {', '.join(manifest.categories)}.",
                },
            }
        result: dict[str, Any] = {
            "enabled": True,
            "integration": integration,
            "already_enabled": not outcome.changed,
            "tools": [
                {"name": o.tool_name, "description": o.description, "access": _access(o)}
                for o in outcome.exposed
            ],
            "available_from": "your next turn",
        }
        if outcome.dispatched:
            result["via_integration_call"] = [
                {"operation": o.action, "description": o.description, "access": _access(o)}
                for o in outcome.dispatched
            ]
        if outcome.unknown_categories:
            result["unknown_categories"] = list(outcome.unknown_categories)
        return result

    def disable(self, integration: Any) -> dict[str, Any]:
        if not isinstance(integration, str) or not self.scope.disable(integration):
            return {
                "disabled": False,
                "error": {
                    "code": ErrorCode.NOT_FOUND.value,
                    "message": "That integration is not enabled.",
                },
            }
        return {"disabled": True, "integration": integration, "removed_from": "your next turn"}
