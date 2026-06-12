"""Context assembly with canonical section ordering and budget enforcement.

The ContextAssembler composes chunks into a final context string that respects
budget constraints, enforces canonical ordering, and prioritizes trimming of
low-trust, low-priority content.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Sequence

from velune.context.budget import ContextBudget
from velune.context.sections import (
    ContextAssemblyReport,
    ContextChunk,
    ContextSection,
)
from velune.context.token_counter import TokenCounter
from velune.core.types.model import ModelDescriptor

logger = logging.getLogger("velune.context.assembler")


class ContextAssembler:
    """Assembles context chunks into final context respecting budget limits.

    Algorithm:
    1. Group chunks by section
    2. Sort groups by ContextSection (canonical order 1-7)
    3. For RETRIEVED_CONTEXT: trim to fit retrieval_allocation (drop low trust_score)
    4. For WORKING_MEMORY: trim oldest turns to fit working_memory_allocation
    5. For REPOSITORY_SNAPSHOT: reduce to architecture summary if > 2000 tokens
    6. NEVER trim: SYSTEM_PROMPT, ARCHITECTURAL_DRIFT, CURRENT_PROMPT
    7. Join with clear section separators
    """

    SECTION_SEPARATOR = "--- SECTION: {name} ({number} of 7) ---"
    SECTION_END = "--- END SECTION ---"
    UNTRIMMED_SECTIONS = {
        ContextSection.SYSTEM_PROMPT,
        ContextSection.ARCHITECTURAL_DRIFT,
        ContextSection.CURRENT_PROMPT,
    }

    def assemble(
        self,
        chunks: Sequence[ContextChunk],
        budget: ContextBudget,
        model: ModelDescriptor | None = None,
    ) -> tuple[str, ContextAssemblyReport]:
        """Assemble chunks into final context respecting budget.

        Args:
            chunks: List of ContextChunk objects to assemble
            budget: ContextBudget constraints for this session
            model: Optional ModelDescriptor for token counting (defaults to word count)

        Returns:
            Tuple of (assembled_context_string, assembly_report)
        """
        if not chunks:
            return "", ContextAssemblyReport(
                total_chunks_received=0,
                total_tokens_requested=budget.usable_tokens,
                total_tokens_assembled=0,
            )

        # Phase 1: Group chunks by section (preserves insertion order)
        sections_dict: dict[ContextSection, list[ContextChunk]] = defaultdict(list)
        for chunk in chunks:
            sections_dict[chunk.section].append(chunk)

        # Phase 2: Process each section in canonical order
        assembled_sections: dict[ContextSection, str] = {}
        total_tokens = 0
        sections_trimmed: dict[ContextSection, int] = {}

        for section in sorted(ContextSection):
            if section not in sections_dict:
                continue

            section_chunks = sections_dict[section]
            is_trimmable = section not in self.UNTRIMMED_SECTIONS

            # Apply section-specific trimming logic
            if section == ContextSection.RETRIEVED_CONTEXT:
                processed_chunks, tokens_trimmed = self._trim_retrieved_context(
                    section_chunks, budget.retrieval_allocation, model
                )
                if tokens_trimmed > 0:
                    sections_trimmed[section] = tokens_trimmed
            elif section == ContextSection.WORKING_MEMORY:
                processed_chunks, tokens_trimmed = self._trim_working_memory(
                    section_chunks, budget.working_memory_allocation, model
                )
                if tokens_trimmed > 0:
                    sections_trimmed[section] = tokens_trimmed
            elif section == ContextSection.REPOSITORY_SNAPSHOT:
                processed_chunks = self._trim_repository_snapshot(section_chunks)
            elif is_trimmable:
                # For other trimmable sections, no special logic yet
                processed_chunks = section_chunks
            else:
                # Never trim SYSTEM_PROMPT, ARCHITECTURAL_DRIFT, CURRENT_PROMPT
                processed_chunks = section_chunks

            if processed_chunks:
                section_content = self._render_section(section, processed_chunks)
                assembled_sections[section] = section_content
                total_tokens += sum(c.token_count for c in processed_chunks)

        # Phase 3: Render final context with separators
        final_context = self._render_assembled_context(assembled_sections)

        # Phase 4: Check if final assembly exceeds budget
        if model:
            final_token_count = TokenCounter.count(final_context, model)
        else:
            final_token_count = total_tokens

        budget_exceeded = final_token_count > budget.usable_tokens

        if budget_exceeded:
            logger.warning(
                f"Assembled context exceeds budget: "
                f"{final_token_count} > {budget.usable_tokens}; "
                f"dropping lowest-priority RETRIEVED_CONTEXT chunks"
            )
            # Emergency truncation of RETRIEVED_CONTEXT
            if ContextSection.RETRIEVED_CONTEXT in assembled_sections:
                del assembled_sections[ContextSection.RETRIEVED_CONTEXT]
                final_context = self._render_assembled_context(assembled_sections)

        report = ContextAssemblyReport(
            total_chunks_received=len(chunks),
            total_tokens_requested=budget.usable_tokens,
            total_tokens_assembled=final_token_count,
            sections_present=list(assembled_sections.keys()),
            sections_trimmed=sections_trimmed,
            chunks_dropped=len(chunks)
            - sum(len(assembled_sections.get(s, [])) for s in ContextSection),
            budget_exceeded=budget_exceeded,
        )

        return final_context, report

    def _trim_retrieved_context(
        self,
        chunks: list[ContextChunk],
        allocation: int,
        model: ModelDescriptor | None = None,
    ) -> tuple[list[ContextChunk], int]:
        """Trim RETRIEVED_CONTEXT to fit allocation.

        Drops lowest trust_score chunks first. Sorts by trust_score descending
        and keeps chunks until allocation is exceeded.
        """
        if sum(c.token_count for c in chunks) <= allocation:
            return chunks, 0

        # Sort by trust_score descending (keep high-trust chunks first)
        sorted_chunks = sorted(chunks, key=lambda c: c.trust_score, reverse=True)

        kept_chunks = []
        tokens_used = 0
        for chunk in sorted_chunks:
            if tokens_used + chunk.token_count <= allocation:
                kept_chunks.append(chunk)
                tokens_used += chunk.token_count

        tokens_trimmed = sum(c.token_count for c in chunks) - sum(
            c.token_count for c in kept_chunks
        )
        return kept_chunks, tokens_trimmed

    def _trim_working_memory(
        self,
        chunks: list[ContextChunk],
        allocation: int,
        model: ModelDescriptor | None = None,
    ) -> tuple[list[ContextChunk], int]:
        """Trim WORKING_MEMORY to fit allocation.

        Removes oldest turns (first chunks) first to preserve recent context.
        """
        if sum(c.token_count for c in chunks) <= allocation:
            return chunks, 0

        # Keep chunks from the end (most recent turns) first
        kept_chunks = []
        tokens_used = 0
        for chunk in reversed(chunks):
            if tokens_used + chunk.token_count <= allocation:
                kept_chunks.insert(0, chunk)
                tokens_used += chunk.token_count

        tokens_trimmed = sum(c.token_count for c in chunks) - sum(
            c.token_count for c in kept_chunks
        )
        return kept_chunks, tokens_trimmed

    def _trim_repository_snapshot(self, chunks: list[ContextChunk]) -> list[ContextChunk]:
        """Reduce REPOSITORY_SNAPSHOT to architecture summary if over 2000 tokens.

        Currently returns chunks as-is. Future enhancement: extract and preserve
        only high-level architecture info when exceeding 2000 tokens.
        """
        total_tokens = sum(c.token_count for c in chunks)
        if total_tokens > 2000:
            logger.debug(
                f"REPOSITORY_SNAPSHOT exceeds 2000 tokens ({total_tokens}); "
                "consider extracting architecture summary only"
            )
        return chunks

    def _render_section(self, section: ContextSection, chunks: list[ContextChunk]) -> str:
        """Render a section with header, content, and footer."""
        section_name = section.name
        section_number = section.value

        header = self.SECTION_SEPARATOR.format(name=section_name, number=section_number)
        content = "\n\n".join(c.content for c in chunks)
        footer = self.SECTION_END

        return f"{header}\n{content}\n{footer}"

    def _render_assembled_context(self, sections: dict[ContextSection, str]) -> str:
        """Render all sections in canonical order with separators."""
        rendered = []
        for section in sorted(ContextSection):
            if section in sections:
                rendered.append(sections[section])

        return "\n\n".join(rendered)
