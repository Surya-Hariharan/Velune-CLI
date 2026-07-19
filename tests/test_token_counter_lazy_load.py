"""tiktoken must load lazily, not at import time.

Loading the cl100k_base encoding (regex compile + merge-rank table) costs
~150-200ms of CPU — real on a weak single-thread CPU — and used to run
unconditionally the moment `velune.context` was imported, which happens on
nearly every CLI invocation via context assembly. That cost was paid even for
a session that only ever talks to a local Ollama model and never needs
tiktoken at all. These tests pin down that both the plain `import tiktoken`
and the more expensive `get_encoding()` call are now deferred to first actual
use, and that the counting behavior itself is unchanged.
"""

from __future__ import annotations

import importlib
import sys

import pytest


def _fresh_token_counter_module():
    """Import token_counter with tiktoken (and it) purged from sys.modules,
    isolating this test from whatever earlier tests already imported."""
    for name in list(sys.modules):
        if name == "tiktoken" or name.startswith("tiktoken."):
            del sys.modules[name]
        if name == "velune.context.token_counter":
            del sys.modules[name]
    import velune.context.token_counter as tc

    return importlib.reload(tc)


def test_importing_module_does_not_load_tiktoken():
    tc = _fresh_token_counter_module()
    assert "tiktoken" not in sys.modules
    assert tc._tiktoken_load_attempted is False
    assert tc._ENCODING_LOAD_ATTEMPTED is False


def test_estimate_tokens_lazily_loads_tiktoken_on_first_call():
    tc = _fresh_token_counter_module()
    assert "tiktoken" not in sys.modules

    result = tc.estimate_tokens("hello world, this is a test sentence")

    assert result > 0
    assert "tiktoken" in sys.modules
    assert tc._ENCODING_LOAD_ATTEMPTED is True


def test_estimate_tokens_empty_string_short_circuits_without_loading():
    tc = _fresh_token_counter_module()
    assert tc.estimate_tokens("") == 0
    # Never touched tiktoken for empty input.
    assert tc._ENCODING_LOAD_ATTEMPTED is False


def test_load_failure_is_cached_not_retried_every_call(monkeypatch):
    tc = _fresh_token_counter_module()

    call_count = 0
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def _fake_import(name, *args, **kwargs):
        nonlocal call_count
        if name == "tiktoken":
            call_count += 1
            raise ImportError("simulated: tiktoken not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _fake_import)

    first = tc.estimate_tokens("some text to estimate")
    second = tc.estimate_tokens("more text to estimate here")

    assert first > 0  # heuristic fallback still produces a result
    assert second > 0
    assert call_count == 1, "a failed load must be cached, not retried every call"


def test_token_counter_count_still_works_for_gpt_family():
    from velune.context.token_counter import TokenCounter
    from velune.core.types.model import ModelDescriptor

    model = ModelDescriptor(
        model_id="gpt-4o",
        provider_id="openai",
        display_name="GPT-4o",
        context_length=128000,
        capabilities=None,
    )
    assert TokenCounter.count("hello world", model) > 0


def test_token_counter_count_heuristic_for_non_gpt_family():
    from velune.context.token_counter import TokenCounter
    from velune.core.types.model import ModelDescriptor

    model = ModelDescriptor(
        model_id="llama3.1:8b",
        provider_id="ollama",
        display_name="Llama 3.1 8B",
        context_length=131072,
        capabilities=None,
    )
    # Non-GPT family never touches tiktoken at all.
    assert TokenCounter.count("hello world", model) > 0
