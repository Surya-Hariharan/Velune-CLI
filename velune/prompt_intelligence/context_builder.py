"""ContextBuilder: wraps the existing ContextAssembler, unchanged.

See ``docs/02-prompt-intelligence.md`` §12. ``ContextAssembler`` keeps
ownership of ordering, trust-based trimming, and budget enforcement exactly
as it does today — this module packages its output as :class:`IRContextNode`
objects, it does not fork or reimplement any of that logic.

Today's implementation hands the *entire* assembled context back as one
opaque node rather than one node per ``ContextSection`` — so a renderer's
``context_ordering`` currently only controls whether that single block is
placed before or after the role/constraints/workflow instruction block, not
fine-grained per-section interleaving. Splitting this per section is a
natural follow-up once real reordering needs are validated against
production output (see ``renderers/base.py``'s module note); it is not done
here to avoid speculative complexity ahead of that evidence.
"""

from __future__ import annotations

from collections.abc import Sequence

from velune.context.assembler import ContextAssembler
from velune.context.budget import ContextBudget
from velune.context.sections import ContextAssemblyReport, ContextChunk, ContextSection
from velune.core.types.model import ModelDescriptor
from velune.prompt_intelligence.ir import IRContextNode


class ContextBuilder:
    """Wraps :meth:`ContextAssembler.assemble` as ``PromptIR`` context nodes."""

    def __init__(self, assembler: ContextAssembler | None = None) -> None:
        self._assembler = assembler or ContextAssembler()

    def build(
        self,
        chunks: Sequence[ContextChunk],
        budget: ContextBudget,
        model: ModelDescriptor | None = None,
    ) -> tuple[list[IRContextNode], ContextAssemblyReport]:
        """Assemble *chunks* and wrap the result as IR context nodes.

        Returns the :class:`ContextAssemblyReport` alongside the nodes so a
        caller (the ``PromptCompiler``) can surface ``budget_exceeded``
        without re-deriving it.
        """
        context_str, report = self._assembler.assemble(chunks, budget, model)
        if not context_str:
            return [], report

        min_trust = min((c.trust_score for c in chunks), default=1.0)
        node = IRContextNode(
            section=ContextSection.RETRIEVED_CONTEXT,
            content=context_str,
            trust_score=min_trust,
            is_instruction_bearing=False,
        )
        return [node], report
