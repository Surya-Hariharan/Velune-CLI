"""IR data model invariants — see docs/02-prompt-intelligence.md §7, §16."""

from __future__ import annotations

import pytest

from velune.context.sections import ContextSection
from velune.prompt_intelligence.ir import (
    IRContextNode,
    PromptIR,
)


def test_prompt_ir_requires_nonempty_role():
    with pytest.raises(ValueError, match="role"):
        PromptIR(role="", current_request="do something")


def test_prompt_ir_requires_nonempty_current_request():
    with pytest.raises(ValueError, match="current_request"):
        PromptIR(role="You are an assistant.", current_request="")


def test_prompt_ir_minimal_construction_succeeds():
    ir = PromptIR(role="You are an assistant.", current_request="hello")
    assert ir.role == "You are an assistant."
    assert ir.constraints == []
    assert ir.context_nodes == []


def test_ir_context_node_rejects_out_of_range_trust_score():
    with pytest.raises(ValueError, match="trust_score"):
        IRContextNode(section=ContextSection.RETRIEVED_CONTEXT, content="x", trust_score=1.5)


def test_ir_context_node_defaults_not_instruction_bearing():
    node = IRContextNode(section=ContextSection.RETRIEVED_CONTEXT, content="repo data")
    assert node.is_instruction_bearing is False
