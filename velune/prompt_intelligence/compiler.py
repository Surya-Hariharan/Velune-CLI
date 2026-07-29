"""Prompt Compiler: TaskIntent + context chunks + tools -> CompiledPrompt.

See ``docs/02-prompt-intelligence.md`` §10 (compilation pipeline). Nothing in
Velune's live REPL/council call paths imports this module yet — wiring it in
is a separate, flag-gated migration (design doc §18); this package is a
pure addition.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from typing import Any

from velune.context.budget import ContextBudget
from velune.context.sections import ContextChunk
from velune.core.types.model import ModelDescriptor
from velune.models.family import ModelFamily, detect_family
from velune.prompt_intelligence.context_builder import ContextBuilder
from velune.prompt_intelligence.intent import TaskIntent
from velune.prompt_intelligence.ir import (
    CompilationReport,
    CompiledPrompt,
    Constraint,
    PromptIR,
)
from velune.prompt_intelligence.optimizer import TokenOptimizer
from velune.prompt_intelligence.profiles import get_provider_profile
from velune.prompt_intelligence.renderers.anthropic import AnthropicRenderer
from velune.prompt_intelligence.renderers.base import ProviderRenderer
from velune.prompt_intelligence.renderers.gemini import GeminiRenderer
from velune.prompt_intelligence.renderers.generic import GenericRenderer
from velune.prompt_intelligence.renderers.openai import OpenAIRenderer
from velune.prompt_intelligence.templates import TaskTemplateRegistry
from velune.prompt_intelligence.validator import PromptValidator

logger = logging.getLogger("velune.prompt_intelligence.compiler")

_RENDERERS: dict[ModelFamily, ProviderRenderer] = {
    ModelFamily.CLAUDE: AnthropicRenderer(),
    ModelFamily.GPT: OpenAIRenderer(),
    ModelFamily.GEMINI: GeminiRenderer(),
}
_GENERIC_RENDERER = GenericRenderer()


def register_renderer(family: ModelFamily, renderer: ProviderRenderer) -> None:
    """Extensibility hook — only needed if GenericRenderer + a profile isn't
    enough for a given family (§5, §17)."""
    _RENDERERS[family] = renderer


class PromptCompiler:
    """Orchestrates template resolution, context building, optimization,
    validation, and rendering for one turn."""

    def __init__(
        self,
        templates: TaskTemplateRegistry | None = None,
        context_builder: ContextBuilder | None = None,
        validator: PromptValidator | None = None,
        optimizer: TokenOptimizer | None = None,
    ) -> None:
        self._templates = templates or TaskTemplateRegistry()
        self._context_builder = context_builder or ContextBuilder()
        self._validator = validator or PromptValidator()
        self._optimizer = optimizer or TokenOptimizer()

    def compile(
        self,
        intent: TaskIntent,
        model: ModelDescriptor,
        chunks: Sequence[ContextChunk],
        budget: ContextBudget,
        tools: list[dict[str, Any]] | None = None,
    ) -> CompiledPrompt:
        template = self._templates.get(intent.kind)

        context_nodes, assembly_report = self._context_builder.build(chunks, budget, model)

        ir = PromptIR(
            role=template.role_fragment,
            current_request=intent.request_text,
            constraints=[Constraint(text=c.text, source=c.source) for c in template.constraints],
            context_nodes=context_nodes,
            tool_narrative=_render_tool_narrative(tools) if tools else None,
            workflow=template.workflow,
            output_contract=template.output_contract,
        )

        ir = self._optimizer.dedupe(ir)

        family = detect_family(model.family or model.model_id)
        profile = get_provider_profile(family)

        ir = self._optimizer.fit_to_budget(ir, profile)

        validation = self._validator.validate(ir, profile)
        if validation.has_errors:
            logger.warning(
                "PromptValidator found error-level issues for family=%s: %s",
                family.value,
                [i.message for i in validation.issues if i.severity == "error"],
            )

        renderer = _RENDERERS.get(family, _GENERIC_RENDERER)
        rendered = renderer.render(ir, profile)

        report = CompilationReport(
            template_used=template.kind.value,
            family=family.value,
            renderer=type(renderer).__name__,
            validation=validation,
            budget_exceeded=assembly_report.budget_exceeded,
            ir_hash=_hash_ir(ir),
        )

        return CompiledPrompt(rendered=rendered, report=report)


def compile_prompt(
    intent: TaskIntent,
    model: ModelDescriptor,
    chunks: Sequence[ContextChunk],
    budget: ContextBudget,
    tools: list[dict[str, Any]] | None = None,
) -> CompiledPrompt:
    """Module-level convenience wrapper around ``PromptCompiler().compile()``."""
    return PromptCompiler().compile(intent, model, chunks, budget, tools)


def _render_tool_narrative(tools: list[dict[str, Any]]) -> str | None:
    names = [t.get("function", {}).get("name", "?") for t in tools]
    if not names:
        return None
    return "Available tools: " + ", ".join(names) + "."


def _hash_ir(ir: PromptIR) -> str:
    payload = ir.role + "|" + "|".join(c.text for c in ir.constraints) + "|" + ir.current_request
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
