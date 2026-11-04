"""AgentSpec validation and the AgentRegistry."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from velune.core.types.agent import AgentRole
from velune.council.domain import ArtifactKind, ReadScope
from velune.council.profiles import RoutingRole
from velune.models.specializations import CouncilRole
from velune.orchestration.engine import (
    AgentBudget,
    AgentKind,
    AgentRegistry,
    AgentSpec,
    DuplicateAgentError,
    EngineError,
    ReadGrant,
    ToolPolicy,
    UnknownAgentError,
)


def reasoning(role: AgentRole = AgentRole.PLANNER, **kw) -> AgentSpec:
    kw.setdefault("routing_role", RoutingRole.PLANNER)
    kw.setdefault("prompt_ref", "engine.planner")
    return AgentSpec(role=role, **kw)


def acting(**kw) -> AgentSpec:
    kw.setdefault("tool_policy", ToolPolicy(allowed_tools=("read_file",)))
    return reasoning(AgentRole.CODER, kind=AgentKind.ACTING, routing_role=RoutingRole.CODER, **kw)


# ── AgentSpec ───────────────────────────────────────────────────────────────


def test_a_minimal_reasoning_spec_is_valid_and_has_no_tools():
    spec = reasoning()
    assert spec.kind is AgentKind.REASONING
    assert not spec.tool_policy.grants_tools
    assert spec.budget.timeout_s > 0


def test_a_spec_is_frozen_and_rejects_unknown_fields():
    spec = reasoning()
    with pytest.raises(ValidationError):
        spec.prompt_ref = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        AgentSpec(
            role=AgentRole.PLANNER,
            routing_role=RoutingRole.PLANNER,
            prompt_ref="x",
            provider="openai",  # type: ignore[call-arg]
        )


def test_a_reasoning_agent_cannot_have_tools():
    with pytest.raises(ValidationError, match="reasoning agent cannot have tools"):
        reasoning(tool_policy=ToolPolicy(allowed_tools=("write_file",)))


def test_an_acting_agent_is_representable_and_needs_a_tool():
    assert acting().kind is AgentKind.ACTING
    with pytest.raises(ValidationError, match="at least one allowed tool"):
        reasoning(AgentRole.CODER, kind=AgentKind.ACTING)


def test_tool_policy_rejects_repeats_and_bad_names():
    with pytest.raises(ValidationError, match="repeat"):
        ToolPolicy(allowed_tools=("a", "a"))
    with pytest.raises(ValidationError):
        ToolPolicy(allowed_tools=("has space",))
    with pytest.raises(ValidationError):
        ToolPolicy(allowed_tools=("",))


@pytest.mark.parametrize(
    "field, value",
    [
        ("timeout_s", 0),
        ("timeout_s", -1),
        ("timeout_s", float("inf")),
        ("timeout_s", float("nan")),
        ("timeout_s", 99999),
        ("max_output_tokens", 0),
        ("max_output_tokens", -5),
        ("max_output_tokens", 10**9),
        ("max_input_chars", 0),
        ("max_input_chars", 10**9),
    ],
)
def test_budget_rejects_invalid_values(field, value):
    with pytest.raises(ValidationError):
        AgentBudget(**{field: value})


def test_budget_accepts_sane_bounds():
    budget = AgentBudget(timeout_s=5, max_output_tokens=256, max_input_chars=1000)
    assert (budget.timeout_s, budget.max_output_tokens, budget.max_input_chars) == (5, 256, 1000)


@pytest.mark.parametrize("ref", ["", "Has Caps", "-lead", "a" * 129, "with space"])
def test_prompt_ref_must_be_a_plain_identifier(ref):
    with pytest.raises(ValidationError):
        reasoning(prompt_ref=ref)


def test_read_scope_has_one_grant_per_kind_and_defaults_to_none():
    spec = reasoning(
        read_scope=(
            ReadGrant(kind=ArtifactKind.QUESTION, scope=ReadScope.ALL),
            ReadGrant(kind=ArtifactKind.CRITIQUE, scope=ReadScope.OWN),
        )
    )
    assert spec.scope_for(ArtifactKind.QUESTION) is ReadScope.ALL
    assert spec.scope_for(ArtifactKind.EVIDENCE) is ReadScope.NONE
    with pytest.raises(ValidationError, match="at most one grant"):
        reasoning(
            read_scope=(
                ReadGrant(kind=ArtifactKind.QUESTION, scope=ReadScope.ALL),
                ReadGrant(kind=ArtifactKind.QUESTION, scope=ReadScope.OWN),
            )
        )


def test_routing_role_stays_in_step_with_council_role():
    """The engine uses the council core's provider-free mirror; this keeps it honest."""
    assert {r.value for r in RoutingRole} == {r.value for r in CouncilRole}
    for role in RoutingRole:
        assert reasoning(routing_role=role).council_role is CouncilRole(role.value)


def test_architect_debugger_optimizer_are_not_added_to_council_role():
    assert {r.value for r in CouncilRole} == {
        "planner",
        "coder",
        "reviewer",
        "challenger",
        "synthesizer",
    }


def test_a_spec_round_trips_through_json():
    spec = acting(
        read_scope=(ReadGrant(kind=ArtifactKind.DECISION, scope=ReadScope.ALL),),
        budget=AgentBudget(timeout_s=9, max_output_tokens=100, max_input_chars=500),
    )
    assert AgentSpec.model_validate_json(spec.model_dump_json()) == spec


# ── AgentRegistry ───────────────────────────────────────────────────────────


def test_a_new_registry_is_empty_with_no_implicit_agents():
    registry = AgentRegistry()
    assert len(registry) == 0 and registry.roles() == () and registry.specs() == ()


def test_register_and_look_up_by_role_or_name():
    spec = reasoning()
    registry = AgentRegistry([spec])
    assert registry.get(AgentRole.PLANNER) is spec
    assert registry.get("planner") is spec
    assert AgentRole.PLANNER in registry and "planner" in registry
    assert AgentRole.CODER not in registry and "nope" not in registry


def test_duplicate_registration_is_rejected_and_keeps_the_original():
    first = reasoning()
    registry = AgentRegistry([first])
    with pytest.raises(DuplicateAgentError, match="planner"):
        registry.register(reasoning(prompt_ref="engine.other"))
    assert registry.get("planner") is first and len(registry) == 1


def test_unknown_agents_raise_a_typed_actionable_error():
    registry = AgentRegistry([reasoning()])
    with pytest.raises(UnknownAgentError) as caught:
        registry.get(AgentRole.REVIEWER)
    assert "reviewer" in str(caught.value) and "planner" in str(caught.value)
    assert isinstance(caught.value, (EngineError, LookupError))
    with pytest.raises(UnknownAgentError, match="wizard"):
        registry.get("wizard")
    with pytest.raises(UnknownAgentError, match="registered agents: none"):
        AgentRegistry().get(AgentRole.PLANNER)


def test_specs_is_a_snapshot_and_roles_are_sorted():
    registry = AgentRegistry([reasoning(AgentRole.REVIEWER), reasoning(AgentRole.CODER)])
    assert registry.roles() == ("coder", "reviewer")
    snapshot = registry.specs()
    registry.register(reasoning(AgentRole.PLANNER))
    assert len(snapshot) == 2 and len(registry) == 3
