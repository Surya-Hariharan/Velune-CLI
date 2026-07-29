"""``ProviderRenderer`` ABC + the shared IR-to-messages rendering algorithm.

See ``docs/02-prompt-intelligence.md`` §11. Every renderer implements the
same four steps (order sections, apply delimiter style, apply planning
style, emit final messages); they differ only in *how*, per the
``ProviderProfile`` they're given — most families need only a profile, not a
renderer subclass (§17).

NOTE: today's ``ContextBuilder`` (see ``context_builder.py``) hands over the
entire assembled context as ONE opaque ``IRContextNode`` rather than one node
per ``ContextSection``, so ``context_ordering`` here only controls whether
that single block is placed before or after the role/constraints/workflow
instruction block, not fine-grained per-section interleaving. Splitting
``ContextBuilder`` to preserve per-section nodes is a natural follow-up once
real per-section reordering needs are validated against production output.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from velune.prompt_intelligence.ir import (
    ContextOrdering,
    DelimiterStyle,
    PlanningStyle,
    PromptIR,
    ProviderProfile,
    RenderedPrompt,
)


class ProviderRenderer(ABC):
    """Pure ``PromptIR`` -> ``RenderedPrompt`` transform. No I/O, no side effects."""

    @abstractmethod
    def render(self, ir: PromptIR, profile: ProviderProfile) -> RenderedPrompt: ...


def render_ir(ir: PromptIR, profile: ProviderProfile) -> RenderedPrompt:
    """Shared rendering algorithm used by every concrete renderer."""
    instruction_block = _render_instruction_block(ir, profile)
    context_block = _render_context_block(ir, profile)

    if profile.context_ordering == ContextOrdering.CONTEXT_FIRST:
        parts = [p for p in (context_block, profile.anchor_phrase, instruction_block) if p]
    else:  # STABLE_FIRST or ROLE_TASK_CONTEXT
        parts = [p for p in (instruction_block, context_block) if p]

    system_content = "\n\n".join(parts).strip()
    if profile.max_system_tokens is not None:
        system_content = _truncate_to_tokens(system_content, profile.max_system_tokens)

    messages: list[dict[str, Any]] = []
    if system_content:
        messages.append({"role": "system", "content": system_content})
    messages.append({"role": "user", "content": ir.current_request})

    # -1 = system message, matching InferenceRequest.cache_hints' existing
    # index convention (velune/core/types/inference.py).
    cache_hints = {-1: "ephemeral"} if profile.supports_prompt_caching and system_content else None

    return RenderedPrompt(messages=messages, cache_hints=cache_hints)


def _render_instruction_block(ir: PromptIR, profile: ProviderProfile) -> str:
    sections: list[tuple[str, str]] = [("role", ir.role)]

    if ir.objectives:
        sections.append(("objectives", "\n".join(f"- {o}" for o in ir.objectives)))

    if ir.constraints:
        sections.append(("constraints", "\n".join(f"- {c.text}" for c in ir.constraints)))

    if ir.workflow and ir.workflow.style != PlanningStyle.NONE and ir.workflow.steps:
        if ir.workflow.style == PlanningStyle.EXPLICIT_PHASES:
            body = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(ir.workflow.steps))
        else:  # LIGHT_TOUCH
            body = "\n".join(f"- {s}" for s in ir.workflow.steps)
        sections.append(("workflow", body))

    if ir.tool_narrative:
        sections.append(("tools", ir.tool_narrative))

    if ir.output_contract:
        sections.append(("output_format", ir.output_contract.description))

    if ir.examples:
        body = "\n\n".join(f"Input: {e.input}\nOutput: {e.output}" for e in ir.examples)
        sections.append(("examples", body))

    return _join_sections(sections, profile.delimiter_style)


def _render_context_block(ir: PromptIR, profile: ProviderProfile) -> str:
    if not ir.context_nodes:
        return ""
    body = "\n\n".join(n.content for n in ir.context_nodes)
    return _join_sections([("context", body)], profile.delimiter_style)


def _join_sections(sections: list[tuple[str, str]], style: DelimiterStyle) -> str:
    if not sections:
        return ""
    if style == DelimiterStyle.XML:
        return "\n\n".join(f"<{name}>\n{body}\n</{name}>" for name, body in sections)
    if style == DelimiterStyle.MARKDOWN:
        return "\n\n".join(
            f"## {name.replace('_', ' ').title()}\n{body}" for name, body in sections
        )
    # PLAIN
    return "\n\n".join(f"{name.replace('_', ' ').upper()}:\n{body}" for name, body in sections)


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    max_chars = max_tokens * 4  # same fast estimate ContextAssembler itself uses
    if len(text) <= max_chars:
        return text
    cutoff = text[:max_chars].rfind("\n")
    if cutoff <= 0:
        cutoff = max_chars
    return text[:cutoff] + "\n... [truncated to fit provider system-prompt limit]"
