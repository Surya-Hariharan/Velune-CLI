"""ContextBuilder wraps ContextAssembler unchanged — see
docs/02-prompt-intelligence.md §12.
"""

from __future__ import annotations

from velune.context.budget import ContextBudget
from velune.context.sections import ContextChunk, ContextSection
from velune.prompt_intelligence.context_builder import ContextBuilder


def _budget() -> ContextBudget:
    return ContextBudget(
        total_tokens=8000,
        retrieval_allocation=2000,
        working_memory_allocation=2000,
        output_reservation=1000,
    )


def test_empty_chunks_produce_no_context_nodes():
    builder = ContextBuilder()
    nodes, report = builder.build([], _budget())
    assert nodes == []
    assert report.total_chunks_received == 0


def test_nonempty_chunks_wrap_as_single_context_node():
    chunks = [
        ContextChunk(
            section=ContextSection.REPOSITORY_SNAPSHOT,
            content="repo: velune",
            token_count=5,
            source="repo",
        ),
        ContextChunk(
            section=ContextSection.RETRIEVED_CONTEXT,
            content="past turn about auth",
            token_count=6,
            source="memory",
            trust_score=0.6,
        ),
    ]
    builder = ContextBuilder()
    nodes, report = builder.build(chunks, _budget())

    assert len(nodes) == 1
    node = nodes[0]
    assert "repo: velune" in node.content
    assert "past turn about auth" in node.content
    # Trust score is the minimum across input chunks — the whole block is
    # only as trustworthy as its least-trusted contributor.
    assert node.trust_score == 0.6
    assert node.is_instruction_bearing is False
    assert report.total_chunks_received == 2


def test_context_node_never_instruction_bearing_by_default():
    chunks = [
        ContextChunk(
            section=ContextSection.REPOSITORY_SNAPSHOT,
            content="repo data",
            token_count=3,
            source="repo",
        )
    ]
    nodes, _ = ContextBuilder().build(chunks, _budget())
    assert all(not n.is_instruction_bearing for n in nodes)
