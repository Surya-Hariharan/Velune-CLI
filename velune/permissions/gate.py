"""The permission gate: where a proposed action meets the policy and the user.

:func:`velune.tools.base.tool.authorize_and_execute` hands every tool call's
actions to :meth:`PermissionGate.check` before the tool runs. The gate asks the
policy, resolves ASK through the interactive callback, records the decision
for the audit trail, and raises :class:`ActionDeniedError` (a
``PermissionError``) to refuse. Tools never decide for themselves.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from velune._compat import StrEnum
from velune.execution.path_guard import PathTraversalError, resolve_in_workspace
from velune.permissions.actions import Action
from velune.permissions.boundary import Boundary
from velune.permissions.policy import Decision, PolicyState, Verdict, authorize

logger = logging.getLogger("velune.permissions.gate")


class Approval(StrEnum):
    ALLOW_ONCE = "allow_once"
    # Outside-workspace: grant the location for the rest of the task.
    # Otherwise: allow this tool without asking again this session (MANUAL's
    # "always allow"), never for high-risk or secret actions.
    ALLOW_TASK = "allow_task"
    DENY = "deny"


AskFn = Callable[[str, Decision], Awaitable[Approval]]
AskBatchFn = Callable[[list[tuple[str, Decision]]], Awaitable[bool]]
AuditFn = Callable[[dict[str, Any]], None]


def _batch_key(tool_name: str, actions: list[Action]) -> tuple:
    return (tool_name, tuple((a.action_type.value, a.target, a.detail) for a in actions))


def _sensitive(decision: Decision) -> bool:
    return (
        decision.high_risk or decision.outside_workspace or any(a.secret for a in decision.actions)
    )


class ActionDeniedError(PermissionError):
    """A proposed action was refused by the policy or by the user."""

    def __init__(self, tool_name: str, reason: str) -> None:
        super().__init__(f"'{tool_name}' was not allowed: {reason}")
        self.tool_name = tool_name
        self.reason = reason


@dataclass
class PermissionGate:
    state: PolicyState
    boundary: Boundary
    ask: AskFn | None = None
    audit: AuditFn | None = None
    session_grants: set[str] = field(default_factory=set)
    # Set for a turn whose request may have been misread (low intent
    # confidence): session grants then don't skip the human check.
    force_confirm: bool = False
    # Batched approval (MANUAL): several ordinary changes proposed in one
    # model turn are confirmed with a single "approve all?" prompt.
    ask_batch: AskBatchFn | None = None
    _preapproved: set[tuple] = field(default_factory=set)
    _prerejected: set[tuple] = field(default_factory=set)

    async def preauthorize(self, calls: list[tuple[str, list[Action]]]) -> None:
        """Ask once for a turn's related changes instead of once per call.

        Only ordinary ASK decisions are batched; high-risk, outside-workspace
        and secret actions keep their own explicit prompt at execution time.
        """
        if self.ask_batch is None:
            return
        pending: list[tuple[str, Decision, tuple]] = []
        for tool_name, actions in calls:
            decision = authorize(actions, self.state)
            if decision.verdict is not Verdict.ASK or _sensitive(decision):
                continue
            if tool_name in self.session_grants and not self.force_confirm:
                continue
            pending.append((tool_name, decision, _batch_key(tool_name, actions)))
        if len(pending) < 2:
            return
        approved = await self.ask_batch([(name, decision) for name, decision, _ in pending])
        keys = {key for _, _, key in pending}
        if approved:
            self._preapproved |= keys
        else:
            self._prerejected |= keys

    def clear_batch(self) -> None:
        self._preapproved.clear()
        self._prerejected.clear()

    async def check(self, tool_name: str, actions: list[Action]) -> list[Path]:
        """Authorize *actions* for *tool_name*, or raise :class:`ActionDeniedError`.

        Returns the outside-workspace roots the user approved *once*: the
        caller admits them for this single call only.
        """
        decision = authorize(actions, self.state)
        if decision.verdict is Verdict.ALLOW:
            self._record(tool_name, actions, decision, "allowed")
            return []
        if decision.verdict is Verdict.DENY:
            self._record(tool_name, actions, decision, "denied")
            raise ActionDeniedError(tool_name, decision.reason)

        sensitive = _sensitive(decision)
        key = _batch_key(tool_name, actions)
        if key in self._preapproved and not sensitive:
            self._preapproved.discard(key)
            self._record(tool_name, actions, decision, "approved by user (batch)")
            return []
        if key in self._prerejected:
            self._prerejected.discard(key)
            self._record(tool_name, actions, decision, "rejected by user (batch)")
            raise ActionDeniedError(tool_name, "the user rejected it")
        if tool_name in self.session_grants and not sensitive and not self.force_confirm:
            self._record(tool_name, actions, decision, "allowed (session grant)")
            return []
        if self.ask is None:
            self._record(tool_name, actions, decision, "denied (no interactive approval)")
            raise ActionDeniedError(
                tool_name, f"needs approval ({decision.reason}) but no one can be asked"
            )

        answer = await self.ask(tool_name, decision)
        if answer is Approval.DENY:
            self._record(tool_name, actions, decision, "rejected by user")
            raise ActionDeniedError(tool_name, "the user rejected it")
        outside_targets = [Path(a.target) for a in decision.actions if a.outside_workspace]
        once: list[Path] = []
        if answer is Approval.ALLOW_TASK:
            if outside_targets:
                for target in outside_targets:
                    self.boundary.grant(target)
            elif not sensitive:
                self.session_grants.add(tool_name)
        else:
            once = [Boundary.root_for(t) for t in outside_targets]
        self._record(tool_name, actions, decision, f"approved by user ({answer.value})")
        return once

    def _record(
        self, tool_name: str, actions: list[Action], decision: Decision, outcome: str
    ) -> None:
        if self.audit is None:
            return
        try:
            self.audit(
                {
                    "mode": self.state.mode.value,
                    "plan_executing": self.state.plan_executing,
                    "tool": tool_name,
                    "actions": [
                        {
                            "type": a.action_type.value,
                            "target": a.target,
                            "risk": a.risk.value,
                            "outside_workspace": a.outside_workspace,
                        }
                        for a in actions
                    ],
                    "decision": decision.verdict.value,
                    "reason": decision.reason,
                    "outcome": outcome,
                }
            )
        except Exception as exc:  # auditing must never break a tool call
            logger.debug("permission audit hook failed: %s", exc)


# The boundary of the gate authorizing the current tool call. Tools resolve
# their paths through :func:`resolve_tool_path`, so a location the user
# granted for this task is usable, while everything else stays confined.
_ACTIVE_BOUNDARY: contextvars.ContextVar[Boundary | None] = contextvars.ContextVar(
    "velune_active_boundary", default=None
)


def set_active_boundary(boundary: Boundary | None) -> contextvars.Token:
    return _ACTIVE_BOUNDARY.set(boundary)


def reset_active_boundary(token: contextvars.Token) -> None:
    _ACTIVE_BOUNDARY.reset(token)


def resolve_tool_path(raw: str | Path, workspace: Path, label: str = "path") -> Path:
    """Resolve a tool's path argument against the session boundary.

    Under a gate, paths may also lie in a directory the user granted for this
    task; without one (tests, legacy callers) the workspace-only rule applies.
    """
    boundary = _ACTIVE_BOUNDARY.get()
    if boundary is None:
        return resolve_in_workspace(raw, workspace, label=label)
    resolved = boundary.resolve(raw)
    if not boundary.inside(resolved):
        raise PathTraversalError(
            f"{label}: '{raw}' resolves to '{resolved}', outside the workspace "
            f"'{boundary.workspace_root}' and not granted for this task."
        )
    return resolved
