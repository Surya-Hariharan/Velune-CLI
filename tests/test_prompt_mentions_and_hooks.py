"""Regression tests for VeluneREPL._handle_prompt's mention + hook wiring.

Previously this block imported ``velune.context.mention_resolver.MentionResolver``
(module doesn't exist) and called ``self._hook_dispatcher.dispatch_pre_prompt``
(method doesn't exist) — both errors were swallowed by a bare
``except Exception: _log.debug(...)``, so @file mentions, the
UserPromptSubmit hook, and auto-lint of mentioned files were silently dead
on every prompt.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from velune.cli.repl import VeluneREPL
from velune.hooks.types import HookResult


def _make_repl(workspace, hook_result=None):
    repl = VeluneREPL.__new__(VeluneREPL)  # bypass heavyweight __init__
    repl.console = MagicMock()
    repl.active_model = MagicMock(provider_id="local")

    provider_registry = MagicMock()
    provider_registry.get.return_value = MagicMock()

    def _container_get(key):
        return {
            "runtime.provider_registry": provider_registry,
            "runtime.workspace": str(workspace),
        }.get(key)

    repl.container = MagicMock()
    repl.container.get.side_effect = _container_get
    repl._conversation = []
    repl._episodic_session_id = None
    repl._hook_dispatcher = MagicMock()
    repl._hook_dispatcher.session_id = "sess"
    repl._hook_dispatcher.dispatch_user_prompt = AsyncMock(
        return_value=hook_result or HookResult(blocked=True, block_reason="stop-for-test")
    )
    return repl


async def test_file_mention_is_resolved_and_injected(tmp_path, caplog):
    mentioned = tmp_path / "notes.txt"
    mentioned.write_text("the secret ingredient is basil", encoding="utf-8")
    repl = _make_repl(tmp_path)

    caplog.set_level("DEBUG", logger="velune.cli.repl")
    await repl._handle_prompt(f"summarize @{mentioned.name}")

    assert not any("Mention resolution error" in r.message for r in caplog.records)
    system_messages = [m["content"] for m in repl._conversation if m["role"] == "system"]
    assert any("the secret ingredient is basil" in m for m in system_messages)


async def test_dispatch_user_prompt_called_with_cleaned_text(tmp_path):
    repl = _make_repl(tmp_path)
    await repl._handle_prompt("hello there")

    repl._hook_dispatcher.dispatch_user_prompt.assert_awaited_once()
    _, kwargs = repl._hook_dispatcher.dispatch_user_prompt.call_args
    assert kwargs["user_prompt"] == "hello there"
    assert kwargs["session_id"] == "sess"


async def test_hook_system_message_is_injected_into_conversation(tmp_path):
    repl = _make_repl(
        tmp_path,
        hook_result=HookResult(blocked=True, system_message="heads up: low disk space"),
    )
    await repl._handle_prompt("hello there")

    system_messages = [m["content"] for m in repl._conversation if m["role"] == "system"]
    assert "heads up: low disk space" in system_messages


async def test_hook_block_reason_is_printed_and_prompt_not_recorded(tmp_path):
    repl = _make_repl(
        tmp_path, hook_result=HookResult(blocked=True, block_reason="policy violation")
    )
    await repl._handle_prompt("do something risky")

    printed = " ".join(str(c.args[0]) for c in repl.console.print.call_args_list)
    assert "policy violation" in printed
    assert not any(m["role"] == "user" for m in repl._conversation)
