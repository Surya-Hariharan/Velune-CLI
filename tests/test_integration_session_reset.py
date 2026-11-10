"""A new conversation starts without the previous one's enabled integrations (VAIR A2).

The model has no memory of having enabled anything once the conversation is reset,
so carrying its tools over would be unexplained and cost tokens for nothing. The
wiring is tested through the real `/new` and workspace-switch handlers.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

from tests.integration_fakes import make_env
from velune.cli.handlers.tool_chat import reset_integration_tools


class Container:
    def __init__(self, workspace: Path):
        self._services = {
            "runtime.workspace": str(workspace),
            "runtime.working_memory": SimpleNamespace(clear=lambda: None),
        }

    def get(self, key):
        return self._services[key]


async def noop():
    return None


def fake_repl(tmp_path: Path, source) -> SimpleNamespace:
    return SimpleNamespace(
        console=Console(file=io.StringIO(), force_terminal=False, width=100),
        container=Container(tmp_path),
        _conversation=[{"role": "user", "content": "hi"}],
        session_tokens=5,
        session_cost=1.0,
        _context_tracker=SimpleNamespace(update=lambda conversation: None),
        _end_episodic_session=noop,
        _start_episodic_session=noop,
        _archive_current_session=lambda: None,
        _load_project_profile=lambda: None,
        _project_profile=None,
        _workspace_registry=SimpleNamespace(touch=lambda path: None),
        _integration_tools=source,
    )


async def enabled_source(tmp_path: Path):
    env = make_env(tmp_path)
    await env.source.enable("fakeapp")
    assert env.source.scope.enabled_ids() == ["fakeapp"]
    return env.source


async def test_the_helper_clears_the_enabled_integrations(tmp_path):
    source = await enabled_source(tmp_path)
    version = source.scope.version
    reset_integration_tools(fake_repl(tmp_path, source))
    assert source.scope.enabled_ids() == [] and source.scope.exposed == ()
    assert source.scope.version > version  # the next request carries the smaller tool list


def test_the_helper_is_a_no_op_without_a_source(tmp_path):
    reset_integration_tools(fake_repl(tmp_path, None))
    reset_integration_tools(SimpleNamespace())  # a REPL that never built one


async def test_new_session_resets_the_enabled_integrations(tmp_path):
    from velune.cli.handlers.session import cmd_new

    source = await enabled_source(tmp_path)
    repl = fake_repl(tmp_path, source)
    await cmd_new(repl, "")
    assert repl._conversation == [] and source.scope.enabled_ids() == []


async def test_switching_workspace_resets_the_enabled_integrations(tmp_path, monkeypatch):
    from velune.cli.handlers.workspace import switch_workspace

    async def fake_switch(container, new_path):
        return ["switched"]

    monkeypatch.setattr("velune.cli.workspaces.switch_workspace", fake_switch)
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    source = await enabled_source(tmp_path)
    repl = fake_repl(old, source)
    await switch_workspace(repl, new)
    assert repl._conversation == [] and source.scope.enabled_ids() == []


async def test_clear_only_clears_the_screen_and_keeps_the_conversation_tools(tmp_path, monkeypatch):
    from velune.cli.handlers.session import cmd_clear

    monkeypatch.setattr("builtins.print", lambda *a, **k: None)
    source = await enabled_source(tmp_path)
    repl = fake_repl(tmp_path, source)
    await cmd_clear(repl, "")
    assert source.scope.enabled_ids() == ["fakeapp"] and repl._conversation
