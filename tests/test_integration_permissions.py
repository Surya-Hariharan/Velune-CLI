"""Policy for INTEGRATION_* actions: scopes, fail-closed rules, modes, audit (VAIR A1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from velune.permissions.actions import (
    MUTATING_TYPES,
    Action,
    ActionType,
    Risk,
    scope_segment,
    valid_scope,
)
from velune.permissions.boundary import Boundary
from velune.permissions.gate import ActionDeniedError, Approval, PermissionGate
from velune.permissions.policy import Decision, ExecutionMode, PolicyState, Verdict, authorize


def _act(kind=ActionType.INTEGRATION_WRITE, scope="docker.container.start", **kw) -> Action:
    return Action(kind, "docker:start", "test", kw.pop("risk", Risk.MEDIUM), scope=scope, **kw)


def _verdict(action: Action, mode=ExecutionMode.MANUAL, *, executing=False) -> Verdict:
    return authorize([action], PolicyState(mode=mode, plan_executing=executing)).verdict


# ── Scope validation ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "scope",
    ["docker.container.start", "http.request.read", "database.query.write", "mcp.fs.read_file"],
)
def test_valid_scopes(scope):
    assert valid_scope(scope)


@pytest.mark.parametrize(
    "scope",
    [
        "",
        "docker",  # needs at least two segments
        "Docker.container.start",  # lowercase only
        "docker.*",  # no wildcards: matching is exact
        "docker..start",
        "docker.container.start ",
        "docker.container.start;rm",
        "x." + "a" * 200,  # length cap
    ],
)
def test_invalid_scopes(scope):
    assert not valid_scope(scope)


def test_scope_segment_normalizes_untrusted_names():
    assert scope_segment("My Server!") == "my_server"
    assert scope_segment("..") == "x"
    assert valid_scope(f"mcp.{scope_segment('ACME/tools')}.{scope_segment('_hidden')}")


# ── Fail-closed rules ───────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", list(ExecutionMode))
@pytest.mark.parametrize(
    "kind",
    [
        ActionType.INTEGRATION_READ,
        ActionType.INTEGRATION_WRITE,
        ActionType.INTEGRATION_EXECUTE,
        ActionType.INTEGRATION_DELETE,
    ],
)
def test_missing_or_invalid_scope_is_denied_in_every_mode(mode, kind):
    for scope in ("", "not a scope"):
        assert _verdict(_act(kind, scope=scope), mode) is Verdict.DENY


@pytest.mark.parametrize("mode", list(ExecutionMode))
def test_scope_on_a_non_integration_action_is_denied(mode):
    action = Action(ActionType.READ, "/x", scope="docker.inspect")
    assert _verdict(action, mode) is Verdict.DENY


@pytest.mark.parametrize("mode", list(ExecutionMode))
def test_blocked_integration_action_is_denied(mode):
    assert _verdict(_act(blocked=True), mode) is Verdict.DENY


# ── Mode semantics ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", list(ExecutionMode))
def test_integration_read_is_allowed_in_every_mode(mode):
    assert _verdict(_act(ActionType.INTEGRATION_READ, "docker.inspect"), mode) is Verdict.ALLOW


def test_integration_read_of_secrets_asks():
    action = _act(ActionType.INTEGRATION_READ, "database.query.read", secret=True)
    assert _verdict(action) is Verdict.ASK


@pytest.mark.parametrize("kind", [ActionType.INTEGRATION_WRITE, ActionType.INTEGRATION_EXECUTE])
def test_medium_changes_follow_the_mode_table(kind):
    action = _act(kind)
    assert _verdict(action, ExecutionMode.MANUAL) is Verdict.ASK
    assert _verdict(action, ExecutionMode.PLAN) is Verdict.DENY
    assert _verdict(action, ExecutionMode.AUTO) is Verdict.ALLOW
    # Plan scope names files, never integration operations: executing asks.
    assert _verdict(action, ExecutionMode.PLAN, executing=True) is Verdict.ASK


def test_high_risk_change_asks_even_in_auto():
    assert _verdict(_act(risk=Risk.HIGH), ExecutionMode.AUTO) is Verdict.ASK


def test_delete_asks_in_auto_even_at_medium_risk():
    action = _act(ActionType.INTEGRATION_DELETE, "docker.volume.delete", risk=Risk.MEDIUM)
    assert action.high_risk
    assert _verdict(action, ExecutionMode.AUTO) is Verdict.ASK
    assert _verdict(action, ExecutionMode.PLAN) is Verdict.DENY


def test_auto_never_changes_outside_the_workspace():
    action = _act(outside_workspace=True)
    assert _verdict(action, ExecutionMode.AUTO) is Verdict.DENY
    assert _verdict(action, ExecutionMode.MANUAL) is Verdict.ASK


def test_integration_types_are_mutating_except_read():
    assert ActionType.INTEGRATION_READ not in MUTATING_TYPES
    for kind in (
        ActionType.INTEGRATION_WRITE,
        ActionType.INTEGRATION_EXECUTE,
        ActionType.INTEGRATION_DELETE,
    ):
        assert kind in MUTATING_TYPES


def test_existing_actions_are_unaffected():
    """Backwards compatibility: non-integration actions decide exactly as before."""
    state = PolicyState(mode=ExecutionMode.AUTO)
    assert authorize([Action(ActionType.READ, "/a")], state).verdict is Verdict.ALLOW
    assert authorize([Action(ActionType.NETWORK, "u")], state).verdict is Verdict.ALLOW
    assert (
        authorize([Action(ActionType.RUN_COMMAND, "x", risk=Risk.HIGH)], state).verdict
        is Verdict.ASK
    )
    manual = PolicyState(mode=ExecutionMode.MANUAL)
    assert authorize([Action(ActionType.NETWORK, "u")], manual).verdict is Verdict.ASK
    # Positional construction keeps working with the new trailing field.
    legacy = Action(ActionType.RUN_COMMAND, "make", "build", Risk.MEDIUM)
    assert legacy.scope == "" and not legacy.integration


def test_decision_high_risk_covers_integration_delete():
    d = Decision(Verdict.ASK, "x", (_act(ActionType.INTEGRATION_DELETE, "docker.volume.delete"),))
    assert d.high_risk


# ── Gate: audit and session grants ──────────────────────────────────────────


def _gate(mode=ExecutionMode.MANUAL, ask=None) -> tuple[PermissionGate, list[dict]]:
    records: list[dict] = []
    gate = PermissionGate(
        state=PolicyState(mode=mode),
        boundary=Boundary(Path.cwd()),
        ask=ask,
        audit=records.append,
    )
    return gate, records


async def test_gate_audit_records_the_scope():
    gate, records = _gate(ExecutionMode.AUTO)
    await gate.check("docker__start", [_act()])
    assert records[-1]["actions"][0]["scope"] == "docker.container.start"
    assert records[-1]["actions"][0]["type"] == "INTEGRATION_WRITE"
    assert records[-1]["outcome"] == "allowed"


async def test_gate_audit_omits_scope_for_ordinary_actions():
    gate, records = _gate()
    await gate.check("read_file", [Action(ActionType.READ, "/a")])
    assert "scope" not in records[-1]["actions"][0]


async def test_unscoped_integration_action_is_denied_and_audited():
    gate, records = _gate(ExecutionMode.AUTO)
    with pytest.raises(ActionDeniedError):
        await gate.check("docker__start", [_act(scope="")])
    assert records[-1]["outcome"] == "denied"


async def test_session_grant_never_covers_an_integration_delete():
    asked: list[str] = []

    async def ask(tool, decision):
        asked.append(tool)
        return Approval.ALLOW_TASK

    gate, _ = _gate(ask=ask)
    delete = _act(ActionType.INTEGRATION_DELETE, "docker.volume.delete")
    await gate.check("docker__volume_rm", [delete])
    await gate.check("docker__volume_rm", [delete])
    assert asked == ["docker__volume_rm", "docker__volume_rm"]
    assert "docker__volume_rm" not in gate.session_grants


async def test_session_grant_covers_an_ordinary_integration_write():
    asked: list[str] = []

    async def ask(tool, decision):
        asked.append(tool)
        return Approval.ALLOW_TASK

    gate, _ = _gate(ask=ask)
    await gate.check("docker__restart", [_act()])
    await gate.check("docker__restart", [_act()])
    assert asked == ["docker__restart"]
