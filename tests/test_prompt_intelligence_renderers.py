"""Provider renderers — see docs/02-prompt-intelligence.md §11.

Each renderer implements the same four steps (order sections, apply
delimiter style, apply planning style, emit final messages) differing only
in *how*, per the ProviderProfile supplied — these tests exercise the
shared algorithm through each of the four concrete renderer classes.
"""

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
    WorkflowSpec,
)
from velune.prompt_intelligence.profiles import (
    ANTHROPIC_PROFILE,
    GEMINI_PROFILE,
    GENERIC_PROFILE,
    OPENAI_PROFILE,
)
from velune.prompt_intelligence.renderers.anthropic import AnthropicRenderer
from velune.prompt_intelligence.renderers.gemini import GeminiRenderer
from velune.prompt_intelligence.renderers.generic import GenericRenderer
from velune.prompt_intelligence.renderers.openai import OpenAIRenderer


def _ir(**overrides) -> PromptIR:
    defaults = {
        "role": "You are a helpful assistant.",
        "current_request": "please help",
        "constraints": [Constraint(text="Be concise.", source="template")],
        "context_nodes": [
            IRContextNode(section=ContextSection.RETRIEVED_CONTEXT, content="some context")
        ],
    }
    defaults.update(overrides)
    return PromptIR(**defaults)


def test_anthropic_renderer_uses_xml_delimiters():
    rendered = AnthropicRenderer().render(_ir(), ANTHROPIC_PROFILE)
    system = rendered.messages[0]["content"]
    assert "<role>" in system and "</role>" in system
    assert "<constraints>" in system
    assert "<context>" in system


def test_anthropic_renderer_places_role_before_context_and_populates_cache_hints():
    rendered = AnthropicRenderer().render(_ir(), ANTHROPIC_PROFILE)
    system = rendered.messages[0]["content"]
    assert system.index("<role>") < system.index("<context>")
    assert rendered.cache_hints == {-1: "ephemeral"}


def test_openai_renderer_uses_markdown_headers_and_no_cache_hints():
    rendered = OpenAIRenderer().render(_ir(), OPENAI_PROFILE)
    system = rendered.messages[0]["content"]
    assert "## Role" in system
    assert "## Constraints" in system
    assert rendered.cache_hints is None


def test_gemini_renderer_places_context_before_role_with_anchor_phrase():
    rendered = GeminiRenderer().render(_ir(), GEMINI_PROFILE)
    system = rendered.messages[0]["content"]
    assert system.index("<context>") < system.index(GEMINI_PROFILE.anchor_phrase)
    assert system.index(GEMINI_PROFILE.anchor_phrase) < system.index("<role>")


def test_generic_renderer_uses_plain_delimiters():
    rendered = GenericRenderer().render(_ir(), GENERIC_PROFILE)
    system = rendered.messages[0]["content"]
    assert "ROLE:" in system
    assert "<role>" not in system
    assert "##" not in system


def test_current_request_always_becomes_the_trailing_user_message():
    ir = _ir(current_request="what is the login bug?")
    for renderer, profile in (
        (AnthropicRenderer(), ANTHROPIC_PROFILE),
        (OpenAIRenderer(), OPENAI_PROFILE),
        (GeminiRenderer(), GEMINI_PROFILE),
        (GenericRenderer(), GENERIC_PROFILE),
    ):
        rendered = renderer.render(ir, profile)
        assert rendered.messages[-1] == {"role": "user", "content": "what is the login bug?"}


def test_explicit_phases_workflow_renders_as_numbered_list():
    ir = _ir(workflow=WorkflowSpec(style=PlanningStyle.EXPLICIT_PHASES, steps=("plan", "execute")))
    rendered = AnthropicRenderer().render(ir, ANTHROPIC_PROFILE)
    system = rendered.messages[0]["content"]
    assert "1. plan" in system
    assert "2. execute" in system


def test_light_touch_workflow_renders_as_bullets():
    ir = _ir(workflow=WorkflowSpec(style=PlanningStyle.LIGHT_TOUCH, steps=("understand", "edit")))
    rendered = OpenAIRenderer().render(ir, OPENAI_PROFILE)
    system = rendered.messages[0]["content"]
    assert "- understand" in system
    assert "- edit" in system


def test_none_planning_style_omits_workflow_section_even_if_steps_present():
    ir = _ir(workflow=WorkflowSpec(style=PlanningStyle.NONE, steps=("should", "not", "appear")))
    rendered = GenericRenderer().render(ir, GENERIC_PROFILE)
    system = rendered.messages[0]["content"]
    assert "should" not in system


def test_max_system_tokens_truncates_oversized_system_content():
    tiny_profile = ProviderProfile(
        family="test",
        delimiter_style=DelimiterStyle.PLAIN,
        context_ordering=ContextOrdering.ROLE_TASK_CONTEXT,
        planning_style=PlanningStyle.NONE,
        max_system_tokens=10,  # 40 chars
        supports_prompt_caching=False,
    )
    ir = _ir(role="x" * 500)
    rendered = GenericRenderer().render(ir, tiny_profile)
    system = rendered.messages[0]["content"]
    assert len(system) < 500
    assert "truncated" in system


def test_no_context_nodes_still_produces_a_valid_render():
    ir = _ir(context_nodes=[])
    rendered = GenericRenderer().render(ir, GENERIC_PROFILE)
    assert rendered.messages[0]["role"] == "system"
    assert "context" not in rendered.messages[0]["content"].lower()
