"""The permission policy: proposed actions + current mode → ALLOW / DENY / ASK.

Pure and side-effect free so every rule is unit-testable. The tool layer
(:func:`velune.tools.base.tool.authorize_and_execute`) calls :func:`authorize`
before any tool runs; ASK is resolved by an interactive callback, DENY becomes
an error result the model sees.

==============  =====  ================================  ==========  =================
mode            read   write/create/delete/move/cmd      high-risk   outside workspace
==============  =====  ================================  ==========  =================
MANUAL          allow  ask                               ask         ask
PLAN            allow  deny (plan files only)            deny        deny
PLAN executing  allow  allow if in approved plan, else   ask         ask
                       ask (scope change)
AUTO            allow  allow                             ask         ask
==============  =====  ================================  ==========  =================

Secret files (``.env``, keys, ``~/.ssh``) are high-risk even to *read*.
Blocked commands (``sudo``, disk formatting…) are denied in every mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from velune._compat import StrEnum
from velune.permissions.actions import Action, ActionType, Risk


class ExecutionMode(StrEnum):
    MANUAL = "manual"
    PLAN = "plan"
    AUTO = "auto"

    @property
    def label(self) -> str:
        return self.value.upper()

    def next(self) -> ExecutionMode:
        """Shift+Tab order: MANUAL → PLAN → AUTO → MANUAL."""
        order = [ExecutionMode.MANUAL, ExecutionMode.PLAN, ExecutionMode.AUTO]
        return order[(order.index(self) + 1) % len(order)]


class Verdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass
class PolicyState:
    """What the policy needs to know about the session."""

    mode: ExecutionMode = ExecutionMode.MANUAL
    # Set once the user approves a plan: the agent then executes it, limited
    # to the files the plan named.
    plan_executing: bool = False
    plan_scope: frozenset[Path] = field(default_factory=frozenset)
    plans_dir: Path | None = None


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str
    # The actions the user must confirm (for ASK) or that were refused (DENY).
    actions: tuple[Action, ...] = ()

    @property
    def high_risk(self) -> bool:
        return any(a.risk is Risk.HIGH for a in self.actions)

    @property
    def outside_workspace(self) -> bool:
        return any(a.outside_workspace for a in self.actions)


def _in_plans_dir(action: Action, state: PolicyState) -> bool:
    if state.plans_dir is None:
        return False
    target = Path(action.target)
    plans = state.plans_dir.resolve()
    return target == plans or plans in target.parents


def _in_plan_scope(action: Action, state: PolicyState) -> bool:
    target = Path(action.target)
    return any(target == p or p in target.parents for p in state.plan_scope)


def _decide_one(action: Action, state: PolicyState) -> tuple[Verdict, str]:
    if action.blocked:
        return Verdict.DENY, f"blocked in every mode: {action.reason or action.target}"

    if action.action_type is ActionType.WRITE_PLAN:
        if _in_plans_dir(action, state):
            return Verdict.ALLOW, "plan file"
        return Verdict.DENY, "plans can only be written under .velune/plans/"

    sensitive = action.secret or action.outside_workspace or action.risk is Risk.HIGH

    if action.action_type is ActionType.READ:
        if action.secret:
            return Verdict.ASK, "reads a credentials/secret file"
        if action.outside_workspace:
            return Verdict.ASK, "reads outside the workspace"
        return Verdict.ALLOW, "read-only"

    if action.action_type is ActionType.NETWORK:
        # Fetching or calling a remote service changes nothing locally, but it
        # sends data out: confirm unless the user delegated with AUTO.
        if state.mode is ExecutionMode.AUTO and not sensitive:
            return Verdict.ALLOW, "network access (auto)"
        return Verdict.ASK, "network access"

    # Everything below changes state.
    if state.mode is ExecutionMode.PLAN and not state.plan_executing:
        return (
            Verdict.DENY,
            "plan mode: no changes are made while planning — record this step in the "
            "plan with write_plan; it runs after the user approves the plan",
        )
    if action.secret:
        return Verdict.ASK, "changes a credentials/secret file"
    if action.outside_workspace:
        return Verdict.ASK, "outside the workspace"
    if action.risk is Risk.HIGH:
        return Verdict.ASK, f"high-risk: {action.reason or action.describe()}"

    if state.mode is ExecutionMode.AUTO:
        return Verdict.ALLOW, "auto mode"
    if state.mode is ExecutionMode.PLAN:  # executing an approved plan
        if action.action_type is ActionType.RUN_COMMAND or _in_plan_scope(action, state):
            return Verdict.ALLOW, "part of the approved plan"
        return Verdict.ASK, "not in the approved plan (scope change)"
    return Verdict.ASK, "manual mode: changes need approval"


def authorize(actions: list[Action] | tuple[Action, ...], state: PolicyState) -> Decision:
    """Decide on a batch of actions proposed by one tool call (or one edit set)."""
    if not actions:
        return Decision(Verdict.ALLOW, "nothing to authorize")
    denied: list[tuple[Action, str]] = []
    asked: list[tuple[Action, str]] = []
    for action in actions:
        verdict, reason = _decide_one(action, state)
        if verdict is Verdict.DENY:
            denied.append((action, reason))
        elif verdict is Verdict.ASK:
            asked.append((action, reason))
    if denied:
        return Decision(Verdict.DENY, denied[0][1], tuple(a for a, _ in denied))
    if asked:
        reasons = sorted({r for _, r in asked})
        return Decision(Verdict.ASK, "; ".join(reasons), tuple(a for a, _ in asked))
    return Decision(Verdict.ALLOW, "allowed")
