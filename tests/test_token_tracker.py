"""Tests for the token usage tracking system."""

import pytest

from velune.telemetry.token_tracker import SessionUsage, TokenUsage


def test_groq_cost_is_zero():
    u = TokenUsage.from_response("groq", "llama-3.3-70b-versatile", 1000, 500)
    assert u.cost_usd == 0.0


def test_anthropic_cost_calculates():
    u = TokenUsage.from_response("anthropic", "claude-haiku-4-5", 1000, 500)
    assert u.cost_usd == pytest.approx((1500 / 1000) * 0.00025, rel=1e-4)


def test_openai_gpt4o_mini_cost():
    u = TokenUsage.from_response("openai", "gpt-4o-mini", 2000, 500)
    expected = (2500 / 1000) * 0.00015
    assert u.cost_usd == pytest.approx(expected, rel=1e-4)


def test_unknown_provider_defaults_to_zero_cost():
    u = TokenUsage.from_response("nonexistent_provider", "some-model", 500, 200)
    assert u.cost_usd == 0.0


def test_token_counts_are_correct():
    u = TokenUsage.from_response("groq", "llama-3.3-70b-versatile", 300, 150)
    assert u.prompt_tokens == 300
    assert u.completion_tokens == 150
    assert u.total_tokens == 450


def test_session_accumulates():
    s = SessionUsage()
    s.add(TokenUsage.from_response("groq", "llama-3.3-70b-versatile", 100, 50))
    s.add(TokenUsage.from_response("groq", "llama-3.3-70b-versatile", 200, 100))
    assert s.total_tokens == 450


def test_session_prompt_completion_split():
    s = SessionUsage()
    s.add(TokenUsage.from_response("groq", "llama-3.3-70b-versatile", 100, 50))
    s.add(TokenUsage.from_response("groq", "llama-3.3-70b-versatile", 200, 100))
    assert s.prompt_tokens == 300
    assert s.completion_tokens == 150


def test_session_summary_line_free():
    s = SessionUsage()
    s.add(TokenUsage.from_response("groq", "llama-3.3-70b-versatile", 500, 250))
    line = s.summary_line()
    assert "free" in line
    assert "750" in line  # total tokens


def test_session_summary_line_paid():
    s = SessionUsage()
    s.add(TokenUsage.from_response("anthropic", "claude-haiku-4-5", 1000, 500))
    line = s.summary_line()
    assert "$" in line
    assert "1,500" in line


def test_session_by_provider():
    s = SessionUsage()
    s.add(TokenUsage.from_response("groq", "llama-3.3-70b-versatile", 100, 50))
    s.add(TokenUsage.from_response("openai", "gpt-4o-mini", 200, 100))
    by_provider = s.by_provider()
    assert by_provider["groq"] == 150
    assert by_provider["openai"] == 300


def test_session_empty_cost():
    s = SessionUsage()
    assert s.total_tokens == 0
    assert s.total_cost == 0.0


def test_session_mixed_free_and_paid():
    s = SessionUsage()
    s.add(TokenUsage.from_response("groq", "llama-3.3-70b-versatile", 1000, 500))
    s.add(TokenUsage.from_response("openai", "gpt-4o", 500, 200))
    assert s.total_cost == pytest.approx((700 / 1000) * 0.005, rel=1e-4)
    assert s.total_tokens == 2200
