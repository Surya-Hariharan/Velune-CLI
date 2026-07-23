"""Edit-and-resend: Up/Down cycling through previously sent chat prompts.

PromptRecallState is the pure state machine wired to the live prompt-toolkit
buffer in VeluneREPL._build_fullscreen_ui (Up/Down key bindings) — see that
module's docstring for why it's separated out (unit-testable without a
running Application).
"""

from __future__ import annotations

from velune.cli.prompt_recall import PromptRecallState


def test_up_on_empty_buffer_recalls_most_recent_prompt():
    recall = PromptRecallState()
    prompts = ["first message", "second message", "third message"]

    result = recall.recall_up("", prompts)

    assert result == "third message"
    assert recall.index == 2


def test_repeated_up_walks_further_back():
    recall = PromptRecallState()
    prompts = ["first", "second", "third"]

    first = recall.recall_up("", prompts)
    second = recall.recall_up(first, prompts)
    third = recall.recall_up(second, prompts)

    assert [first, second, third] == ["third", "second", "first"]


def test_up_stops_at_oldest_prompt():
    recall = PromptRecallState()
    prompts = ["only one"]

    recall.recall_up("", prompts)
    result = recall.recall_up("only one", prompts)

    assert result == "only one"
    assert recall.index == 0


def test_up_on_nonempty_unrelated_text_falls_back_to_none():
    """Buffer has something the user typed, not a recalled prompt — Up
    should defer to normal cursor-move/FileHistory behavior."""
    recall = PromptRecallState()
    prompts = ["first", "second"]

    result = recall.recall_up("something I'm typing", prompts)

    assert result is None
    assert recall.index is None


def test_down_after_up_returns_to_more_recent_prompt():
    recall = PromptRecallState()
    prompts = ["first", "second", "third"]

    text = recall.recall_up("", prompts)  # -> "third", index 2
    text = recall.recall_up(text, prompts)  # -> "second", index 1
    text = recall.recall_down(text, prompts)  # -> back to "third"

    assert text == "third"
    assert recall.index == 2


def test_down_past_the_newest_prompt_clears_the_buffer_and_exits_recall():
    recall = PromptRecallState()
    prompts = ["first", "second"]

    text = recall.recall_up("", prompts)  # -> "second"
    text = recall.recall_down(text, prompts)  # -> exits, clears

    assert text == ""
    assert recall.index is None


def test_down_when_not_recalling_falls_back_to_none():
    recall = PromptRecallState()
    result = recall.recall_down("whatever is typed", ["first"])
    assert result is None


def test_editing_the_recalled_text_breaks_out_of_recall_mode():
    """Bash-like: once the recalled line no longer matches (the user edited
    it), the next Up starts fresh from the newest prompt rather than
    resuming the old position."""
    recall = PromptRecallState()
    prompts = ["first", "second", "third"]

    recall.recall_up("", prompts)  # -> "third", index 2
    recall.recall_up("third", prompts)  # -> "second", index 1

    # User edits the recalled text.
    edited = "second, but edited"
    assert recall.still_recalling(edited, prompts) is False

    # A fresh Up now starts over from the newest prompt again.
    result = recall.recall_up(edited, prompts)
    assert result is None  # buffer is non-empty and doesn't match a snapshot


def test_recall_index_survives_a_new_turn_appended_since():
    """Conversation history only grows by appending at the end, so a recall
    index taken before a new turn was sent still points at the same message
    afterward — appending never shifts earlier positions."""
    recall = PromptRecallState()
    prompts = ["first", "second"]
    recall.recall_up("", prompts)  # -> "second", index 1

    grown = ["first", "second", "third"]  # a new turn was sent meanwhile
    assert recall.still_recalling("second", grown) is True

    # Up from here moves one further back, from index 1 to index 0 — "third"
    # (the newly-sent prompt, now the actual newest) is never visited again
    # once recall has already anchored on an older message.
    result = recall.recall_up("second", grown)
    assert result == "first"


def test_empty_prompt_history_is_a_safe_no_op():
    recall = PromptRecallState()
    assert recall.recall_up("", []) is None
    assert recall.recall_down("", []) is None
