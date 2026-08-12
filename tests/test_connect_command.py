"""`/connect` command handler: grammar (single optional provider-id argument)
and the mixed "command + leftover message" carry-over path.

Regression coverage: `/connect` used to take only the first whitespace-split
token as the provider id and silently discard everything else — so
`/connect anthropic explain how oauth works` dropped "explain how oauth
works" on the floor instead of preserving it (the same class of bug fixed for
`/model` — see cli/handlers/model.py's leftover-carryover path).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from velune.cli.handlers import providers as handler


class _Repl:
    def __init__(self):
        self.console = SimpleNamespace(print=lambda *_a, **_k: None)
        self._pending_prompt_text = ""


@pytest.mark.asyncio
async def test_bare_connect_opens_the_add_flow_with_no_leftover(monkeypatch):
    repl = _Repl()
    palette = AsyncMock()
    monkeypatch.setattr(handler, "_palette", lambda _repl: palette)

    await handler.cmd_login(repl, "")

    palette.run.assert_awaited_once_with()
    assert repl._pending_prompt_text == ""


@pytest.mark.asyncio
async def test_connect_with_a_recognized_provider_id_skips_the_picker(monkeypatch):
    repl = _Repl()
    palette = AsyncMock()
    monkeypatch.setattr(handler, "_palette", lambda _repl: palette)

    await handler.cmd_login(repl, "anthropic")

    palette.run.assert_awaited_once_with("anthropic")
    assert repl._pending_prompt_text == ""


@pytest.mark.asyncio
async def test_connect_preserves_leftover_text_after_a_recognized_provider_id(monkeypatch):
    """`/connect anthropic explain how oauth works` — "anthropic" is the
    command's only argument; the rest is the user's real message."""
    repl = _Repl()
    palette = AsyncMock()
    monkeypatch.setattr(handler, "_palette", lambda _repl: palette)

    await handler.cmd_login(repl, "anthropic explain how oauth works")

    palette.run.assert_awaited_once_with("anthropic")
    assert repl._pending_prompt_text == "explain how oauth works"


@pytest.mark.asyncio
async def test_connect_with_unrecognized_first_token_treats_it_all_as_leftover(monkeypatch):
    """Not every `/connect <text>` is `<provider-id> <message>` — if the first
    word isn't a real provider, the whole string is free text, not a bad
    provider id to fail on."""
    repl = _Repl()
    palette = AsyncMock()
    monkeypatch.setattr(handler, "_palette", lambda _repl: palette)

    await handler.cmd_login(repl, "please help me set up a provider")

    palette.run.assert_awaited_once_with()
    assert repl._pending_prompt_text == "please help me set up a provider"


@pytest.mark.asyncio
async def test_connect_never_calls_the_inference_layer_directly():
    import inspect

    src = inspect.getsource(handler)
    for forbidden in ("provider.chat(", "provider.stream(", "InferenceRequest("):
        assert forbidden not in src, f"providers handler module must never call {forbidden}"
