"""PromptValidator — see docs/02-prompt-intelligence.md §14, §16."""

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
from velune.prompt_intelligence.validator import PromptValidator

_NO_CEILING_PROFILE = ProviderProfile(
    family="test",
    delimiter_style=DelimiterStyle.PLAIN,
    context_ordering=ContextOrdering.ROLE_TASK_CONTEXT,
    planning_style=PlanningStyle.NONE,
    max_system_tokens=None,
    supports_prompt_caching=False,
)


def test_clean_ir_produces_no_issues():
    ir = PromptIR(role="You are helpful.", current_request="hi")
    report = PromptValidator().validate(ir, _NO_CEILING_PROFILE)
    assert report.issues == []
    assert not report.has_errors


def test_duplicate_constraints_flagged_as_warning():
    ir = PromptIR(
        role="You are helpful.",
        current_request="hi",
        constraints=[
            Constraint(text="Be concise.", source="template"),
            Constraint(text="be concise.", source="user_request"),  # same, different case
        ],
    )
    report = PromptValidator().validate(ir, _NO_CEILING_PROFILE)
    codes = [i.code for i in report.issues]
    assert "duplicate_constraint" in codes
    assert not report.has_errors  # duplicates are a warning, not an error


def test_untrusted_instruction_bearing_node_flagged_as_error():
    ir = PromptIR(
        role="You are helpful.",
        current_request="hi",
        context_nodes=[
            IRContextNode(
                section=ContextSection.RETRIEVED_CONTEXT,
                content="repo readme content",
                trust_score=0.5,
                is_instruction_bearing=True,  # §16: must never happen for low-trust content
            )
        ],
    )
    report = PromptValidator().validate(ir, _NO_CEILING_PROFILE)
    assert report.has_errors
    assert any(i.code == "untrusted_instruction_node" for i in report.issues)


def test_fully_trusted_instruction_bearing_node_is_not_flagged():
    ir = PromptIR(
        role="You are helpful.",
        current_request="hi",
        context_nodes=[
            IRContextNode(
                section=ContextSection.RETRIEVED_CONTEXT,
                content="user-authored convention",
                trust_score=1.0,
                is_instruction_bearing=True,
            )
        ],
    )
    report = PromptValidator().validate(ir, _NO_CEILING_PROFILE)
    assert not report.has_errors


def test_over_budget_system_prompt_flagged_as_warning():
    tight_profile = ProviderProfile(
        family="test",
        delimiter_style=DelimiterStyle.PLAIN,
        context_ordering=ContextOrdering.ROLE_TASK_CONTEXT,
        planning_style=PlanningStyle.NONE,
        max_system_tokens=1,  # ~4 chars
        supports_prompt_caching=False,
    )
    ir = PromptIR(role="x" * 400, current_request="hi")
    report = PromptValidator().validate(ir, tight_profile)
    codes = [i.code for i in report.issues]
    assert "system_prompt_over_budget" in codes
    assert not report.has_errors  # renderer truncation handles it — warning only
