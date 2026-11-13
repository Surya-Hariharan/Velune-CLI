"""The bridge from resource actions to Velune's central authorization pipeline.

A resource action runs only as a :class:`ResourceOperationTool` passed through
:func:`velune.tools.base.tool.authorize_and_execute`. That one entry point is
what every native tool uses: permission scopes, the execution-mode
:class:`~velune.permissions.gate.PermissionGate` (PLAN / MANUAL / AUTO, session
grants, batching, the audit trail) and the PreToolUse / PostToolUse hooks. The
connector *describes* the call; the gate *decides*. There is no second approver.

Tool names follow ``<resource>__<action>`` — the naming A2 exposes to the model —
so a session "always allow" grant is precise to one integration operation.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from velune.tools.base.tool import BaseTool

if TYPE_CHECKING:
    from velune.resources.base import ResourceConnector
    from velune.resources.ownership import OwnershipLedger

_NAME_SAFE = re.compile(r"[^a-zA-Z0-9_-]")


def _covers(a: Any, b: Any) -> bool:
    """True when action *a* is at least as strictly gated as *b* (same scope)."""
    from velune.permissions.actions import ActionType, Risk

    type_rank = {
        ActionType.INTEGRATION_READ: 0,
        ActionType.INTEGRATION_WRITE: 1,
        ActionType.INTEGRATION_EXECUTE: 1,
        ActionType.INTEGRATION_DELETE: 2,
    }
    risk_rank = {Risk.LOW: 0, Risk.MEDIUM: 1, Risk.HIGH: 2}
    return (
        a.scope == b.scope
        and type_rank.get(a.action_type, -1) >= type_rank.get(b.action_type, 99)
        and risk_rank[a.risk] >= risk_rank[b.risk]
        and (a.secret or not b.secret)
        and (a.outside_workspace or not b.outside_workspace)
        and (a.blocked or not b.blocked)
    )


#: Parameters that name a filesystem location (a build context, a compose file).
_PATH_PARAMS = ("path", "file", "context", "compose_file", "dockerfile")


def _mark_outside_paths(actions: list[Any], params: dict[str, Any], boundary: Any) -> list[Any]:
    """Flag every action outside-workspace when a path parameter leaves the boundary.

    The workspace boundary is enforced by the policy (AUTO refuses, MANUAL
    asks); this only makes sure an integration call that *names* a location is
    judged by that rule too. A path that cannot be resolved counts as outside.
    """
    from dataclasses import replace

    if boundary is None:
        return actions
    outside = False
    for key in _PATH_PARAMS:
        value = params.get(key)
        if not isinstance(value, str) or not value:
            continue
        try:
            outside = not boundary.inside(boundary.resolve(value))
        except Exception:
            outside = True
        if outside:
            break
    if not outside:
        return actions
    return [replace(a, outside_workspace=True) for a in actions]


def operation_tool_name(resource_id: str, action: str) -> str:
    return _NAME_SAFE.sub("_", f"{resource_id}__{action}")[:64]


class ResourceOperationTool(BaseTool):
    """One connector action, presented as a tool so it can only run gated.

    ``execute`` is reached exclusively through ``authorize_and_execute``; the
    manager never calls it directly. The ``params`` keyword carries the
    action's parameters unchanged.
    """

    HOOK_TOOL_NAME = None

    def __init__(
        self,
        connector: ResourceConnector,
        action: str,
        *,
        ledger: OwnershipLedger | None = None,
    ) -> None:
        self._connector = connector
        self._action = action
        self._ledger = ledger

    def get_name(self) -> str:
        return operation_tool_name(
            self._connector.resource_id or self._connector.name, self._action
        )

    def get_description(self) -> str:
        cap = self._connector.capability_for(self._action)
        return cap.description if cap is not None else self._action

    def get_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"params": {"type": "object"}}}

    def describe_actions(self, args: dict[str, Any], boundary: Any) -> list[Any]:
        """Delegate to the connector; an empty or failing description fails closed."""
        from velune.permissions.actions import Action, ActionType, Risk
        from velune.resources.base import ResourceConnector

        params = args.get("params") or {}
        try:
            actions = list(
                self._connector.describe_actions(self._action, params, ledger=self._ledger)
            )
            # The framework's own classification is always part of the decision.
            # A connector override can add context or raise risk, but because
            # the gate decides on the union (any DENY / ASK wins), it can never
            # make a call *less* guarded than its declared tier.
            baseline = ResourceConnector.describe_actions(
                self._connector, self._action, params, ledger=self._ledger
            )
            actions += [b for b in baseline if not any(_covers(a, b) for a in actions)]
        except Exception:
            actions = []
        if not actions or not all(getattr(a, "integration", False) for a in actions):
            # Nothing trustworthy to authorize: present an unscoped integration
            # action, which the policy refuses in every mode.
            actions = [
                Action(
                    ActionType.INTEGRATION_EXECUTE,
                    f"{self._connector.name}:{self._action}",
                    "operation could not be described",
                    Risk.HIGH,
                )
            ]
        actions = _mark_outside_paths(actions, params, boundary)
        return actions

    async def execute(self, **kwargs: Any) -> Any:
        return await self._connector.execute(self._action, kwargs.get("params") or {})
