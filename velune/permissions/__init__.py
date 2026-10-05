"""Execution modes and the action-permission policy.

The agent *proposes* actions (create/modify/delete a file, run a command…);
:func:`velune.permissions.policy.authorize` decides ALLOW / DENY / ASK from the
current :class:`ExecutionMode`; only then may a tool mutate anything. The
decision is enforced in code at the tool layer
(:func:`velune.tools.base.tool.authorize_and_execute`), never by prompt text.
"""

from velune.permissions.actions import Action, ActionType, Risk
from velune.permissions.policy import (
    Decision,
    ExecutionMode,
    PolicyState,
    Verdict,
    authorize,
)

__all__ = [
    "Action",
    "ActionType",
    "Decision",
    "ExecutionMode",
    "PolicyState",
    "Risk",
    "Verdict",
    "authorize",
]
