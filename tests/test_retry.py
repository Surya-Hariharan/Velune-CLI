"""Tests for /retry — regenerate the last response, optionally on another model.

The audit's stated harm: "the only recourse for a bad response is a new
prompt" — no way to re-run the same prompt, on the same or a different model.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from velune.cli.handlers.retry import _pop_last_turn, cmd_retry


def _conv(*roles_and_content: tuple[str, str]) -> SimpleNamespace:
    return SimpleNamespace(_conversation=[{"role": r, "content": c} for r, c in roles_and_content])


# ── _pop_last_turn ────────────────────────────────────────────────────────


def test_pops_a_plain_user_assistant_pair():
    repl = _conv(("user", "hello"), ("assistant", "hi there"))
    text = _pop_last_turn(repl)
    assert text == "hello"
    assert repl._conversation == []


def test_walks_past_tool_and_system_entries_between_user_and_assistant():
    repl = _conv(
        ("user", "read the file"),
        ("tool", "● ReadFile(foo.py)"),
        ("assistant", "here's the content"),
    )
    text = _pop_last_turn(repl)
    assert text == "read the file"
    assert repl._conversation == []


def test_leaves_earlier_turns_untouched():
    repl = _conv(
        ("user", "first"),
        ("assistant", "first reply"),
        ("user", "second"),
        ("assistant", "second reply"),
    )
    text = _pop_last_turn(repl)
    assert text == "second"
    assert repl._conversation == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first reply"},
    ]


def test_no_prior_turn_is_a_true_noop():
    repl = _conv()
    assert _pop_last_turn(repl) is None
    assert repl._conversation == []


def test_only_a_user_turn_with_no_reply_is_not_retriable():
    """A prompt still in flight (or one that never got a response) shouldn't
    be silently discarded — /retry only replays a *completed* exchange."""
    repl = _conv(("user", "still thinking about this"))
    text = _pop_last_turn(repl)
    assert text is None
    # Nothing was discarded.
    assert repl._conversation == [{"role": "user", "content": "still thinking about this"}]


# ── cmd_retry ────────────────────────────────────────────────────────────


def _make_repl():
    repl = MagicMock()
    repl.console = MagicMock()
    repl._conversation = [
        {"role": "user", "content": "explain this bug"},
        {"role": "assistant", "content": "it's a race condition"},
    ]
    repl._handle_prompt = AsyncMock()
    return repl


async def test_retry_with_no_args_replays_on_the_current_model():
    repl = _make_repl()

    await cmd_retry(repl, "")

    repl._handle_prompt.assert_awaited_once_with(
        "explain this bug", model_override=None, provider_override=None
    )
    assert repl._conversation == []


async def test_retry_with_a_model_name_overrides_without_switching_active_model():
    repl = _make_repl()
    other_model = MagicMock(model_id="gpt-4o", provider_id="openai")
    repl._require.side_effect = lambda key, label: {
        "runtime.model_registry": MagicMock(get=MagicMock(return_value=other_model)),
        "runtime.provider_registry": MagicMock(get=MagicMock(return_value=MagicMock())),
    }[key]

    await cmd_retry(repl, "gpt-4o")

    args, kwargs = repl._handle_prompt.call_args
    assert args[0] == "explain this bug"
    assert kwargs["model_override"] is other_model
    assert kwargs["provider_override"] is not None
    # A one-turn override must never touch the persisted active model.
    assert not hasattr(repl, "active_model") or repl.active_model != other_model


async def test_retry_with_an_unknown_model_name_does_not_retry():
    repl = _make_repl()
    repl._require.side_effect = lambda key, label: {
        "runtime.model_registry": MagicMock(get=MagicMock(return_value=None)),
        "runtime.provider_registry": MagicMock(),
    }[key]

    await cmd_retry(repl, "not-a-real-model")

    repl._handle_prompt.assert_not_awaited()
    # The turn must not have been popped either — nothing to retry with yet.
    assert repl._conversation == [
        {"role": "user", "content": "explain this bug"},
        {"role": "assistant", "content": "it's a race condition"},
    ]


async def test_retry_with_no_previous_turn_prints_a_message_and_does_nothing():
    repl = _make_repl()
    repl._conversation = []

    await cmd_retry(repl, "")

    repl._handle_prompt.assert_not_awaited()
    repl.console.print.assert_called_once()
