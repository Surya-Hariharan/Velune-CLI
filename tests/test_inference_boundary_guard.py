"""Defense-in-depth at the inference submission boundary.

The REPL's main loop already routes on `text.startswith("/")` before ever
calling `_handle_prompt` (slash commands go to `_handle_slash_command`
instead) — that boundary alone would make this test file's scenario
unreachable in normal operation. This tests the *second*, lower-level gate
inside `_handle_prompt` itself: even if some future caller (a retry path, a
hook, a plugin) invoked it directly with command-shaped text, it must still
refuse rather than build an InferenceRequest and submit it to the provider.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from velune.cli.repl import VeluneREPL


class _FakeConsole:
    def __init__(self):
        self.printed = []

    def print(self, *args, **kwargs):
        self.printed.append(args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", ["/connect", "/model", "/models", "/doctor", "  /connect anthropic"]
)
async def test_slash_command_shaped_text_is_refused_before_model_resolution(text):
    """The guard must fire before `_resolve_active_model_and_provider` is even
    called — proven by never providing one on the fake self."""
    fake_self = SimpleNamespace(console=_FakeConsole())

    # `_resolve_active_model_and_provider` deliberately absent: if the guard
    # didn't return early, this call would raise AttributeError instead of
    # silently "passing" a bypassed guard.
    await VeluneREPL._handle_prompt(fake_self, text)

    assert fake_self.console.printed, "must tell the user why nothing was sent"
    joined = " ".join(str(a) for call in fake_self.console.printed for a in call)
    assert "not sent" in joined.lower() or "command" in joined.lower()


@pytest.mark.asyncio
async def test_ordinary_text_starting_mid_sentence_with_a_slash_character_still_refused():
    """Conservative by design: anything the outer dispatcher would also treat
    as a command attempt (leading '/', regardless of whether it resolves to a
    known command) is refused here too — consistent with the outer loop,
    which already sends unrecognized `/x` commands to an error, never to the
    LLM."""
    fake_self = SimpleNamespace(console=_FakeConsole())
    await VeluneREPL._handle_prompt(fake_self, "/this-is-not-a-real-command")
    assert fake_self.console.printed


@pytest.mark.asyncio
async def test_normal_prompt_text_is_not_refused_by_the_guard():
    """A real prompt must sail past the guard — proven by reaching (and
    failing inside) the next line, `_resolve_active_model_and_provider`,
    rather than being rejected by the guard itself."""
    fake_self = SimpleNamespace(console=_FakeConsole())

    with pytest.raises(AttributeError):
        await VeluneREPL._handle_prompt(fake_self, "Explain how OAuth works")

    assert fake_self.console.printed == [], "the guard must not fire for a normal message"
