"""TokenOptimizer — see docs/02-prompt-intelligence.md §6, §15."""

from __future__ import annotations

from velune.context.sections import ContextSection
from velune.prompt_intelligence.ir import (
    Constraint,
    ContextOrdering,
    DelimiterStyle,
    IRContextNode,
    PlanningStyle,
    PromptIR,
    ProviderProfile,
)
from velune.prompt_intelligence.optimizer import TokenOptimizer

_NO_CEILING_PROFILE = ProviderProfile(
    family="test",
    delimiter_style=DelimiterStyle.PLAIN,
    context_ordering=ContextOrdering.ROLE_TASK_CONTEXT,
    planning_style=PlanningStyle.NONE,
    max_system_tokens=None,
    supports_prompt_caching=False,
)


def test_dedupe_removes_duplicate_constraints_case_insensitively():
    ir = PromptIR(
        role="r",
        current_request="hi",
        constraints=[
            Constraint(text="Be concise.", source="template"),
            Constraint(text="be concise.", source="user_request"),
            Constraint(text="Cite sources.", source="template"),
        ],
    )
    TokenOptimizer().dedupe(ir)
    assert len(ir.constraints) == 2
    assert ir.constraints[0].text == "Be concise."  # first occurrence kept
    assert ir.constraints[1].text == "Cite sources."


def test_dedupe_preserves_order_and_is_a_noop_when_no_duplicates():
    ir = PromptIR(
        role="r",
        current_request="hi",
        constraints=[Constraint(text="A", source="template"), Constraint(text="B", source="template")],
    )
    TokenOptimizer().dedupe(ir)
    assert [c.text for c in ir.constraints] == ["A", "B"]


def test_fit_to_budget_is_noop_when_profile_has_no_ceiling():
    ir = PromptIR(
        role="r",
        current_request="hi",
        context_nodes=[IRContextNode(section=ContextSection.RETRIEVED_CONTEXT, content="x" * 1000)],
    )
    TokenOptimizer().fit_to_budget(ir, _NO_CEILING_PROFILE)
    assert len(ir.context_nodes) == 1


def test_fit_to_budget_keeps_highest_trust_nodes_first():
    tight_profile = ProviderProfile(
        family="test",
        delimiter_style=DelimiterStyle.PLAIN,
        context_ordering=ContextOrdering.ROLE_TASK_CONTEXT,
        planning_style=PlanningStyle.NONE,
        max_system_tokens=10,  # ~40 chars
        supports_prompt_caching=False,
    )
    low_trust = IRContextNode(
        section=ContextSection.RETRIEVED_CONTEXT, content="y" * 30, trust_score=0.2
    )
    high_trust = IRContextNode(
        section=ContextSection.REPOSITORY_SNAPSHOT, content="x" * 30, trust_score=0.9
    )
    ir = PromptIR(role="r", current_request="hi", context_nodes=[low_trust, high_trust])

    TokenOptimizer().fit_to_budget(ir, tight_profile)

    assert high_trust in ir.context_nodes
    assert low_trust not in ir.context_nodes
