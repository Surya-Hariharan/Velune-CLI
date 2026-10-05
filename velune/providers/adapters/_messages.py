"""Shape Velune's internal chat messages for OpenAI-compatible wire formats.

The tool loop annotates failed tool results with ``"is_error": True`` so that
adapters with a native notion of it (Anthropic's ``tool_result.is_error``) can
translate it. The OpenAI chat schema has no such property, and strict
providers reject the whole request: Groq answers
``HTTP 400 'messages.2': property 'is_error' is unsupported`` — which made the
turn after *any* failed tool call fail. Every OpenAI-compatible adapter must
send messages through :func:`openai_messages`.
"""

from __future__ import annotations

from typing import Any

# Keys Velune adds to messages for its own bookkeeping; never part of the
# OpenAI chat schema. The error state is already conveyed to the model by the
# tool result's content ("Error: ...").
_INTERNAL_KEYS = frozenset({"is_error"})


def openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return *messages* without Velune-internal keys (input is not mutated)."""
    if not any(_INTERNAL_KEYS & m.keys() for m in messages):
        return messages
    return [{k: v for k, v in m.items() if k not in _INTERNAL_KEYS} for m in messages]
