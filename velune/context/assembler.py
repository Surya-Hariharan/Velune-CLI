"""Context assembly with canonical section ordering and budget enforcement.

The ContextAssembler composes chunks into a final context string that respects
budget constraints, enforces canonical ordering, and prioritizes trimming of
low-trust, low-priority content.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Sequence

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
        kept_sections_chunks: dict[ContextSection, list[ContextChunk]] = {}
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
                kept_sections_chunks[section] = list(processed_chunks)
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
                if ContextSection.RETRIEVED_CONTEXT in kept_sections_chunks:
                    del kept_sections_chunks[ContextSection.RETRIEVED_CONTEXT]
                final_context = self._render_assembled_context(assembled_sections)

        report = ContextAssemblyReport(
            total_chunks_received=len(chunks),
            total_tokens_requested=budget.usable_tokens,
            total_tokens_assembled=final_token_count,
            sections_present=list(assembled_sections.keys()),
            sections_trimmed=sections_trimmed,
            chunks_dropped=len(chunks)
            - sum(len(kept_sections_chunks.get(s, [])) for s in ContextSection),
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
        """Reduce REPOSITORY_SNAPSHOT to fit within a 2000-token budget.

        Strategy (applied in order until the section fits):

        1. Drop the lowest-priority chunks first (sort by priority ascending).
        2. If a single chunk is still over the budget, truncate its content at
           the token boundary — keeping the opening lines which contain the
           workspace header and recent-changes sections (highest information
           density per token).

        Chunks with priority >= 0.9 are treated as "must-keep" headers and are
        never dropped at step 1 (they may still be truncated at step 2).
        """
        snapshot_token_budget = 2000
        chars_per_token = 4  # fast estimate, same as context_builder
        must_keep_priority = 0.9

        total_tokens = sum(c.token_count for c in chunks)
        if total_tokens <= snapshot_token_budget:
            return chunks

        logger.debug(
            "REPOSITORY_SNAPSHOT: %d tokens exceeds budget (%d); trimming.",
            total_tokens,
            snapshot_token_budget,
        )

        # Step 1: drop low-priority chunks until we fit.
        droppable = sorted(
            [c for c in chunks if c.priority < must_keep_priority],
            key=lambda c: c.priority,  # lowest priority first
        )
        must_keep = [c for c in chunks if c.priority >= must_keep_priority]

        kept = list(must_keep)
        remaining_budget = snapshot_token_budget - sum(c.token_count for c in kept)

        # Add droppable chunks from highest to lowest priority until budget runs out.
        for chunk in reversed(droppable):
            if chunk.token_count <= remaining_budget:
                kept.append(chunk)
                remaining_budget -= chunk.token_count

        if sum(c.token_count for c in kept) <= snapshot_token_budget:
            return kept

        # Step 2: single-chunk truncation as a last resort.
        budget_chars = snapshot_token_budget * chars_per_token
        trimmed: list[ContextChunk] = []
        chars_used = 0
        for chunk in sorted(kept, key=lambda c: -c.priority):
            if chars_used >= budget_chars:
                break
            available = budget_chars - chars_used
            if len(chunk.content) <= available:
                trimmed.append(chunk)
                chars_used += len(chunk.content)
            else:
                # Truncate at the last newline within the available window.
                cutoff = chunk.content[:available].rfind("\n")
                if cutoff <= 0:
                    cutoff = available
                truncated_content = (
                    chunk.content[:cutoff] + "\n... [snapshot truncated to fit context budget]"
                )
                import dataclasses

                trimmed.append(
                    dataclasses.replace(
                        chunk,
                        content=truncated_content,
                        token_count=len(truncated_content) // chars_per_token,
                    )
                )
                chars_used += len(truncated_content)

        logger.debug(
            "REPOSITORY_SNAPSHOT trimmed: %d → %d tokens.",
            total_tokens,
            sum(c.token_count for c in trimmed),
        )
        return trimmed

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
