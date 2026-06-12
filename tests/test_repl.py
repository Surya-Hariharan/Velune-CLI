"""Integration tests for VeluneREPL slash command flow.

All tests run against a fully mocked runtime container — no live providers,
no network calls, no real model registry.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from velune.cli.repl import VeluneREPL

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_runtime(mock_config, temp_workspace):
    runtime = MagicMock()
    runtime.config = mock_config
    runtime.workspace = temp_workspace
    runtime.console = MagicMock()

    # Build mocks once so the same object is returned on every container.get() call
    working_memory = MagicMock(
        get_turns=lambda: [],
        get_recent_turns=lambda n: [],
        session_id="test",
    )
    working_memory.clear = MagicMock()

    _services = {
        "runtime.gpu_info": {"has_gpu": False},
        "runtime.model_registry": MagicMock(list_all=lambda: [], get=lambda x: None),
        "runtime.provider_registry": MagicMock(get=lambda x: None),
        "runtime.council_orchestrator": MagicMock(),
        "runtime.working_memory": working_memory,
        "runtime.episodic_memory": MagicMock(get_turns=lambda s: []),
        "runtime.semantic_memory": MagicMock(),
        "runtime.workspace": temp_workspace,
    }

    container = MagicMock()
    container.get.side_effect = lambda key: _services.get(key, MagicMock())

    runtime.container = container
    return runtime


@pytest.fixture
def repl(mock_runtime):
    return VeluneREPL(mock_runtime)


# ---------------------------------------------------------------------------
# Core command handlers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_help_command_renders(repl):
    await repl._cmd_help("")
    repl.console.print.assert_called()


@pytest.mark.asyncio
async def test_clear_resets_conversation(repl):
    repl._conversation = [{"role": "user", "content": "hello"}]
    await repl._cmd_clear("")
    assert repl._conversation == []


@pytest.mark.asyncio
async def test_exit_raises_system_exit(repl):
    with pytest.raises(SystemExit):
        await repl._cmd_exit("")


@pytest.mark.asyncio
async def test_unknown_slash_command_prints_hint(repl):
    await repl._handle_slash_command("/nonexistent")
    repl.console.print.assert_called()
    output = str(repl.console.print.call_args)
    assert "Unknown" in output or "nonexistent" in output


# ---------------------------------------------------------------------------
# /model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_command_no_models_shows_warning(repl):
    # model_registry.list_all() returns [] — expect a yellow warning
    await repl._cmd_model("")
    repl.console.print.assert_called()
    printed = " ".join(str(c) for c in repl.console.print.call_args_list)
    assert "No models" in printed or "yellow" in printed


@pytest.mark.asyncio
async def test_model_command_direct_switch_unknown(repl):
    from rich.panel import Panel

    await repl._cmd_model("nonexistent-model-xyz")
    # The model-not-found case now renders a structured error Panel.
    calls = repl.console.print.call_args_list
    assert any(isinstance(c.args[0], Panel) for c in calls), (
        "Expected a rich Panel for unknown model"
    )


# ---------------------------------------------------------------------------
# /memory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_stats_renders_table(repl):
    await repl._cmd_memory("stats")
    repl.console.print.assert_called()


@pytest.mark.asyncio
async def test_memory_empty_args_renders_table(repl):
    await repl._cmd_memory("")
    repl.console.print.assert_called()


@pytest.mark.asyncio
async def test_memory_clear_calls_clear(repl):
    await repl._cmd_memory("clear")
    working = repl.container.get("runtime.working_memory")
    working.clear.assert_called_once()
    printed = " ".join(str(c) for c in repl.console.print.call_args_list)
    assert "cleared" in printed.lower()


# ---------------------------------------------------------------------------
# /session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_save_creates_file(repl, tmp_path, monkeypatch):
    from velune.cli import session_manager

    sessions_dir = tmp_path / "sessions"
    monkeypatch.setattr(session_manager, "SESSIONS_DIR", sessions_dir)

    repl._conversation = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    await repl._cmd_session("save")

    saved = list(sessions_dir.glob("*.json"))
    assert len(saved) == 1
    import json

    data = json.loads(saved[0].read_text(encoding="utf-8"))
    assert data["turn_count"] == 2
    assert data["conversation"][0]["content"] == "hello"


@pytest.mark.asyncio
async def test_session_list_shows_table(repl, tmp_path, monkeypatch):
    from velune.cli import session_manager

    sessions_dir = tmp_path / "sessions"
    monkeypatch.setattr(session_manager, "SESSIONS_DIR", sessions_dir)

    # Pre-seed one session so the table has a row
    repl._conversation = [{"role": "user", "content": "seed"}]
    await repl._cmd_session("save")
    repl.console.reset_mock()

    await repl._cmd_session("list")
    repl.console.print.assert_called()


@pytest.mark.asyncio
async def test_session_resume_restores_conversation(repl, tmp_path, monkeypatch):
    from velune.cli import session_manager

    sessions_dir = tmp_path / "sessions"
    monkeypatch.setattr(session_manager, "SESSIONS_DIR", sessions_dir)

    original = [{"role": "user", "content": "original"}]
    repl._conversation = original
    await repl._cmd_session("save")

    # Extract saved ID
    saved_id = list(sessions_dir.glob("*.json"))[0].stem
    repl._conversation = []  # wipe

    await repl._cmd_session(f"resume {saved_id}")
    assert repl._conversation == original


@pytest.mark.asyncio
async def test_session_resume_missing_id(repl, tmp_path, monkeypatch):
    from velune.cli import session_manager

    monkeypatch.setattr(session_manager, "SESSIONS_DIR", tmp_path / "sessions")
    await repl._cmd_session("resume 00000000")
    printed = " ".join(str(c) for c in repl.console.print.call_args_list)
    assert "not found" in printed


@pytest.mark.asyncio
async def test_session_export_creates_markdown(repl, tmp_path, monkeypatch):
    from velune.cli import session_manager

    sessions_dir = tmp_path / "sessions"
    monkeypatch.setattr(session_manager, "SESSIONS_DIR", sessions_dir)

    repl._conversation = [
        {"role": "user", "content": "what is a BST?"},
        {"role": "assistant", "content": "A binary search tree..."},
    ]
    await repl._cmd_session("save")
    saved_id = list(sessions_dir.glob("*.json"))[0].stem

    # Export with explicit ID
    await repl._cmd_session(f"export {saved_id}")
    md_files = list(Path.cwd().glob(f"velune-session-{saved_id}.md"))
    assert md_files, "Expected .md export file in cwd"
    content = md_files[0].read_text(encoding="utf-8")
    assert "what is a BST?" in content
    md_files[0].unlink()  # clean up


# ---------------------------------------------------------------------------
# /context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_indicator_empty_conversation(repl):
    repl._conversation = []
    await repl._cmd_context("")
    printed = " ".join(str(c) for c in repl.console.print.call_args_list)
    assert "No conversation" in printed


@pytest.mark.asyncio
async def test_context_indicator_with_conversation(repl, mock_model_descriptor):
    repl.active_model = mock_model_descriptor  # context_length = 8192
    repl._conversation = [{"role": "user", "content": "hello world"}]
    await repl._cmd_context("")
    printed = " ".join(str(c) for c in repl.console.print.call_args_list)
    assert "Context:" in printed
    assert "8,192" in printed


# ---------------------------------------------------------------------------
# Prompt token generation
# ---------------------------------------------------------------------------


def test_prompt_tokens_no_model(repl):
    repl.active_model = None
    tokens = repl._get_prompt_tokens()
    text = "".join(t[1] for t in tokens)
    assert "velune" in text
    assert "›" in text


def test_prompt_tokens_with_model_no_conversation(repl, mock_model_descriptor):
    repl.active_model = mock_model_descriptor
    repl._conversation = []
    tokens = repl._get_prompt_tokens()
    text = "".join(t[1] for t in tokens)
    assert "test-model" in text
    # No bar when conversation is empty
    assert "█" not in text and "░" not in text


def test_prompt_tokens_shows_context_bar(repl, mock_model_descriptor):
    repl.active_model = mock_model_descriptor
    repl._conversation = [{"role": "user", "content": "hello"}]
    tokens = repl._get_prompt_tokens()
    text = "".join(t[1] for t in tokens)
    # Bar characters must appear once there is conversation content
    assert "█" in text or "░" in text
    assert "%" in text
