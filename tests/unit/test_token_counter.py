"""Tests for TokenCounter."""

from velune.context.token_counter import TokenCounter
from velune.core.types.model import ModelDescriptor


def test_token_counter_count_empty():
    """Test counting empty text."""
    model = ModelDescriptor(
        model_id="test-model",
        provider_id="test",
        display_name="Test",
        context_length=4096,
        capabilities={},
    )

    count = TokenCounter.count("", model)
    assert count == 0


def test_token_counter_count_text():
    """Test counting non-empty text."""
    model = ModelDescriptor(
        model_id="test-model",
        provider_id="test",
        display_name="Test",
        context_length=4096,
        capabilities={},
    )

    # Should use heuristic: word_count * 1.35
    text = "hello world test"
    count = TokenCounter.count(text, model)

    # 3 words * 1.35 = 4 tokens (with rounding)
    assert count >= 3
    assert count <= 5


def test_token_counter_count_messages():
    """Test counting messages."""
    model = ModelDescriptor(
        model_id="test-model",
        provider_id="test",
        display_name="Test",
        context_length=4096,
        capabilities={},
    )

    messages = [
        {"role": "system", "content": "You are helpful"},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
    ]

    count = TokenCounter.count_messages(messages, model)

    # Should include structure overhead (4 tokens per message) + content
    assert count > 12  # 3 messages * 4 + content


def test_token_counter_encoding_selection():
    """Test encoding selection for different models."""
    # o1 models should use o200k_base
    assert TokenCounter._select_encoding("o1-preview") == "o200k_base"
    assert TokenCounter._select_encoding("o1-mini") == "o200k_base"

    # GPT-4 models should use cl100k_base
    assert TokenCounter._select_encoding("gpt-4-turbo") == "cl100k_base"
    assert TokenCounter._select_encoding("gpt-4") == "cl100k_base"

    # Default should be cl100k_base
    assert TokenCounter._select_encoding("unknown-model") == "cl100k_base"


def test_token_counter_heuristic():
    """Test heuristic token counting."""
    text = "The quick brown fox jumps over the lazy dog"
    tokens = TokenCounter._count_heuristic(text)

    # 9 words * 1.35 ≈ 12 tokens
    assert tokens == 12


def test_token_counter_empty_messages():
    """Test counting empty message list."""
    model = ModelDescriptor(
        model_id="test-model",
        provider_id="test",
        display_name="Test",
        context_length=4096,
        capabilities={},
    )

    count = TokenCounter.count_messages([], model)
    assert count == 0
