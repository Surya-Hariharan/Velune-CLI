"""Cross-source RETRIEVED_CONTEXT deduplication in ContextAssembler.

velune/cli/handlers/prompt_context.py fans out to four retrieval sources
concurrently (hybrid retrieval, the memory-lifecycle fan-out, lineage,
repository snapshot) with no cross-check — hybrid retrieval and the memory
fan-out can and do return the exact same file/turn content. Previously that
duplicate content silently burned budget twice; ContextAssembler now
collapses it before trimming.
"""

from __future__ import annotations

from velune.context.assembler import ContextAssembler
from velune.context.budget import ContextBudget
from velune.context.sections import ContextChunk, ContextSection


def _budget(retrieval_allocation: int = 10_000) -> ContextBudget:
    return ContextBudget(
        total_tokens=20_000,
        retrieval_allocation=retrieval_allocation,
        working_memory_allocation=5_000,
        output_reservation=2_000,
    )


def test_identical_content_from_two_sources_is_collapsed():
    duplicate_text = "def build_widget():\n    return Widget()\n"
    chunks = [
        ContextChunk(
            section=ContextSection.RETRIEVED_CONTEXT,
            content=duplicate_text,
            token_count=20,
            source="hybrid_retrieval:file",
            trust_score=0.7,
            priority=0.5,
        ),
        ContextChunk(
            section=ContextSection.RETRIEVED_CONTEXT,
            content=duplicate_text,
            token_count=20,
            source="semantic_memory",
            trust_score=0.9,
            priority=0.5,
        ),
        ContextChunk(
            section=ContextSection.CURRENT_PROMPT,
            content="fix the widget builder",
            token_count=5,
            source="user",
            trust_score=1.0,
            priority=1.0,
        ),
    ]

    _, report = ContextAssembler().assemble(chunks, _budget())

    assert report.duplicates_dropped == 1
    # chunks_dropped tracks budget-driven trims only, not duplicates.
    assert report.chunks_dropped == 0


def test_deduplication_keeps_the_higher_trust_copy():
    duplicate_text = "the auth middleware validates the bearer token"
    low_trust = ContextChunk(
        section=ContextSection.RETRIEVED_CONTEXT,
        content=duplicate_text,
        token_count=10,
        source="hybrid_retrieval:file",
        trust_score=0.3,
        priority=0.5,
    )
    high_trust = ContextChunk(
        section=ContextSection.RETRIEVED_CONTEXT,
        content=duplicate_text,
        token_count=10,
        source="knowledge_graph",
        trust_score=0.95,
        priority=0.6,
    )

    kept, dropped = ContextAssembler()._deduplicate_retrieved_context([low_trust, high_trust])

    assert dropped == 1
    assert len(kept) == 1
    assert kept[0].source == "knowledge_graph"


def test_whitespace_only_differences_still_collapse():
    kept, dropped = ContextAssembler()._deduplicate_retrieved_context(
        [
            ContextChunk(
                section=ContextSection.RETRIEVED_CONTEXT,
                content="line one\nline two",
                token_count=4,
                source="a",
                trust_score=0.5,
            ),
            ContextChunk(
                section=ContextSection.RETRIEVED_CONTEXT,
                content="line one   line two",
                token_count=4,
                source="b",
                trust_score=0.5,
            ),
        ]
    )

    assert dropped == 1
    assert len(kept) == 1


def test_distinct_content_is_never_dropped():
    chunks = [
        ContextChunk(
            section=ContextSection.RETRIEVED_CONTEXT,
            content="alpha",
            token_count=1,
            source="a",
        ),
        ContextChunk(
            section=ContextSection.RETRIEVED_CONTEXT,
            content="beta",
            token_count=1,
            source="b",
        ),
    ]

    kept, dropped = ContextAssembler()._deduplicate_retrieved_context(chunks)

    assert dropped == 0
    assert len(kept) == 2
