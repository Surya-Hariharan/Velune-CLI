"""Tests for the Claude-family token count approximation.

docs/repository-intelligence-baseline.md §8/Recommendation 6: "Token
counting for Claude is not Claude's tokenizer. TokenCounter routes both
ModelFamily.GPT and ModelFamily.CLAUDE through OpenAI's tiktoken... A
heuristic under-count could let assembled context silently exceed a local
model's real window while Velune's own bookkeeping reports it as under
budget." Anthropic doesn't distribute an offline Claude tokenizer (only a
remote, network-requiring API call), so TokenCounter._count_claude_approx
applies a documented safety margin on top of the tiktoken base count,
biased toward over-counting rather than under-counting.
"""

from __future__ import annotations

from velune.context.token_counter import TokenCounter
from velune.core.types.model import ModelDescriptor

CLAUDE_MODEL = ModelDescriptor(
    model_id="claude-sonnet-5",
    provider_id="anthropic",
    display_name="Claude Sonnet 5",
    context_length=200000,
    capabilities=None,
)
GPT_MODEL = ModelDescriptor(
    model_id="gpt-4-turbo",
    provider_id="openai",
    display_name="GPT-4 Turbo",
    context_length=128000,
    capabilities=None,
)


def test_claude_count_is_higher_than_the_bare_tiktoken_base_count():
    """The whole point of the margin: Claude's count must come out higher
    than GPT's tiktoken-exact count for the same text, not identical to it
    (identical would mean the old silent-reuse bug is still present)."""
    text = "The quick brown fox jumps over the lazy dog. " * 20
    claude_count = TokenCounter.count(text, CLAUDE_MODEL)
    gpt_count = TokenCounter.count(text, GPT_MODEL)
    assert claude_count > gpt_count


def test_claude_margin_matches_the_documented_constant():
    text = "The quick brown fox jumps over the lazy dog. " * 20
    gpt_count = TokenCounter.count(text, GPT_MODEL)
    claude_count = TokenCounter.count(text, CLAUDE_MODEL)
    expected = max(1, int(gpt_count * TokenCounter._CLAUDE_APPROXIMATION_MARGIN))
    assert claude_count == expected


def test_claude_count_never_below_one_for_nonempty_text():
    assert TokenCounter.count("a", CLAUDE_MODEL) >= 1


def test_claude_empty_string_is_zero():
    assert TokenCounter.count("", CLAUDE_MODEL) == 0


def test_claude_count_messages_applies_margin_to_content_not_overhead():
    """count_messages' per-message structure overhead (~4 tokens/message)
    is a fixed accounting convention, not tokenizer output — the margin
    must apply only to the tiktoken-derived content portion."""
    messages = [{"role": "user", "content": "hello world, this is a test"}]
    total = TokenCounter.count_messages(messages, CLAUDE_MODEL)
    content_only = TokenCounter.count(messages[0]["content"], CLAUDE_MODEL)
    assert total == content_only + 4  # 1 message * 4 tokens overhead
