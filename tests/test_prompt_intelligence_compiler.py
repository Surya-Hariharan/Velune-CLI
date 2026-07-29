"""PromptCompiler end-to-end — see docs/02-prompt-intelligence.md §10.

Nothing in Velune's live REPL/council call paths imports this package yet
(§18) — these tests exercise the library's own public contract, not a
golden-parity comparison against production output (that comparison is
Phase 7's job, when a cutover is actually proposed).
"""

from __future__ import annotations

from velune.context.budget import ContextBudget
from velune.context.sections import ContextChunk, ContextSection
from velune.core.types.model import ModelDescriptor
from velune.models.family import ModelFamily
from velune.prompt_intelligence.compiler import PromptCompiler, compile_prompt, register_renderer
from velune.prompt_intelligence.intent import TaskIntent, TaskKind
from velune.prompt_intelligence.ir import Constraint, PlanningStyle, WorkflowSpec
from velune.prompt_intelligence.renderers.generic import GenericRenderer
from velune.prompt_intelligence.templates import TaskTemplate, TaskTemplateRegistry


def _budget() -> ContextBudget:
    return ContextBudget(
        total_tokens=8000,
        retrieval_allocation=2000,
        working_memory_allocation=2000,
        output_reservation=1000,
    )


def _model(model_id: str) -> ModelDescriptor:
    return ModelDescriptor(
        model_id=model_id,
        provider_id="p",
        display_name=model_id,
        context_length=128_000,
        capabilities={},
    )


def _templates() -> TaskTemplateRegistry:
    registry = TaskTemplateRegistry()
    registry.register(
        TaskKind.CODING,
        TaskTemplate(
            kind=TaskKind.CODING,
            role_fragment="You are a careful coding assistant.",
            constraints=(Constraint(text="Make the smallest change.", source="template"),),
            workflow=WorkflowSpec(style=PlanningStyle.LIGHT_TOUCH, steps=("understand", "edit", "verify")),
        ),
    )
    return registry


def test_compile_selects_anthropic_renderer_for_claude_models():
    compiler = PromptCompiler(templates=_templates())
    intent = TaskIntent.from_text("write a new function")
    chunks = [
        ContextChunk(section=ContextSection.REPOSITORY_SNAPSHOT, content="repo: velune", token_count=5, source="repo")
    ]
    result = compiler.compile(intent, _model("claude-sonnet-5"), chunks, _budget())

    assert result.report.renderer == "AnthropicRenderer"
    assert result.report.family == "claude"
    assert result.rendered.cache_hints == {-1: "ephemeral"}
    system = result.rendered.messages[0]["content"]
    assert "<role>" in system
    assert "You are a careful coding assistant." in system


def test_compile_selects_generic_renderer_for_unknown_local_models():
    compiler = PromptCompiler(templates=_templates())
    intent = TaskIntent.from_text("write a new function")
    result = compiler.compile(intent, _model("qwen2.5-coder"), [], _budget())

    assert result.report.renderer == "GenericRenderer"
    assert result.report.family == "qwen"
    assert result.rendered.cache_hints is None


def test_compile_never_produces_an_empty_system_message():
    compiler = PromptCompiler(templates=_templates())
    intent = TaskIntent.from_text("write a new function")
    result = compiler.compile(intent, _model("gpt-4o"), [], _budget())

    assert result.rendered.messages[0]["role"] == "system"
    assert result.rendered.messages[0]["content"].strip()


def test_compile_always_ends_with_the_current_request_as_user_message():
    compiler = PromptCompiler(templates=_templates())
    intent = TaskIntent.from_text("debug the login flow")
    result = compiler.compile(intent, _model("gpt-4o"), [], _budget())

    assert result.rendered.messages[-1] == {"role": "user", "content": "debug the login flow"}


def test_compile_report_has_no_validation_errors_for_a_clean_turn():
    compiler = PromptCompiler(templates=_templates())
    intent = TaskIntent.from_text("write a new function")
    result = compiler.compile(intent, _model("claude-sonnet-5"), [], _budget())

    assert not result.report.validation.has_errors


def test_module_level_compile_prompt_matches_default_compiler():
    intent = TaskIntent.from_text("hello")
    result = compile_prompt(intent, _model("gpt-4o"), [], _budget())
    assert result.rendered.messages[-1] == {"role": "user", "content": "hello"}


def test_register_renderer_is_an_additive_extensibility_hook():
    class _MarkerRenderer(GenericRenderer):
        pass

    register_renderer(ModelFamily.DEEPSEEK, _MarkerRenderer())
    try:
        compiler = PromptCompiler(templates=_templates())
        intent = TaskIntent.from_text("hello")
        result = compiler.compile(intent, _model("deepseek-coder"), [], _budget())
        assert result.report.renderer == "_MarkerRenderer"
    finally:
        from velune.prompt_intelligence.compiler import _RENDERERS

        del _RENDERERS[ModelFamily.DEEPSEEK]
