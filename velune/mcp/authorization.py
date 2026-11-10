"""Central authorization for external MCP tool calls.

An external MCP server is an untrusted integration: its tool names,
descriptions and annotations are asserted by the server, not by Velune. So an
MCP tool call is described as an ``INTEGRATION_*`` action with the scope
``mcp.<server>.<tool>`` and decided by the session's
:class:`~velune.permissions.gate.PermissionGate` through
:func:`~velune.tools.base.tool.authorize_and_execute` — the same path as every
native tool. There is no MCP-specific approver.

Classification never trusts the server to *lower* risk:

=============================  =====================  ======
server annotation              action type            risk
=============================  =====================  ======
(none, or ``readOnlyHint``)    INTEGRATION_EXECUTE    medium
``destructiveHint: true``      INTEGRATION_DELETE     high
=============================  =====================  ======

``readOnlyHint`` is ignored on purpose: honouring it would let any server
declare itself read-only and skip approval. Compared with the previous
behaviour (every MCP call was a plain NETWORK action) this is never weaker:
MANUAL asks and AUTO allows as before; PLAN now refuses instead of asking,
because a planning session makes no changes; a server-declared destructive tool
now always needs a human.
"""

from __future__ import annotations

from typing import Any

from velune.permissions.actions import Action, ActionType, Risk, scope_segment
from velune.tools.base.tool import BaseTool, ToolPermission


def mcp_scope(server_name: str, tool_name: str) -> str:
    return f"mcp.{scope_segment(server_name or 'server')}.{scope_segment(tool_name or 'tool')}"


def describe_mcp_call(
    server_name: str,
    tool_name: str,
    annotations: dict[str, Any] | None = None,
) -> list[Action]:
    """The permission-gate actions for one MCP tool call."""
    annotations = annotations or {}
    destructive = annotations.get("destructiveHint") is True
    target = f"mcp:{server_name}/{tool_name}"
    if destructive:
        return [
            Action(
                ActionType.INTEGRATION_DELETE,
                target,
                "the MCP server declares this tool destructive",
                Risk.HIGH,
                scope=mcp_scope(server_name, tool_name),
            )
        ]
    return [
        Action(
            ActionType.INTEGRATION_EXECUTE,
            target,
            "external MCP tool call",
            Risk.MEDIUM,
            scope=mcp_scope(server_name, tool_name),
        )
    ]


class MCPRegistryTool(BaseTool):
    """One tool of a connected MCP server, routed through the registry.

    Built by :class:`~velune.orchestration.tool_loop.ToolLoopRunner` for every
    MCP tool it exposes. ``execute`` is only reached via
    ``authorize_and_execute``.
    """

    def __init__(self, registry: Any, info: Any, qualified_name: str) -> None:
        self._registry = registry
        self._info = info
        self._qualified = qualified_name

    @property
    def qualified_name(self) -> str:
        return self._qualified

    def get_name(self) -> str:
        return self._qualified

    def get_description(self) -> str:
        return self._info.description or f"MCP tool from {self._info.server_name}"

    def get_schema(self) -> dict[str, Any]:
        return self._info.input_schema or {"type": "object", "properties": {}}

    def get_required_permissions(self) -> set[ToolPermission]:
        # Remote call: read-only auto-approval policies must never match it.
        return {ToolPermission.NETWORK_ACCESS}

    def describe_actions(self, args: dict[str, Any], boundary: Any) -> list[Action]:
        return describe_mcp_call(
            self._info.server_name, self._info.name, getattr(self._info, "annotations", None)
        )

    async def execute(self, **kwargs: Any) -> Any:
        return await self._registry.call_tool(self._qualified, kwargs)
