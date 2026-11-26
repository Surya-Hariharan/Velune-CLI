"""The MCP server must run the council (the agent swarm), or say precisely why it cannot."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from velune.cli.commands import mcp as mcp_commands
from velune.mcp.server import COUNCIL_UNAVAILABLE, VeluneMCPServer


def _outcome(*, ok=True, content="council says hi", error="", notes=()):
    return SimpleNamespace(
        ok=ok,
        content=content if ok else "",
        error=error,
        run_id="run-1",
        plan=SimpleNamespace(notes=list(notes)),
    )


def _council(outcome=None, *, error=None):
    asked: list[str] = []

    async def council(task: str):
        asked.append(task)
        if error is not None:
            raise error
        return outcome or _outcome()

    council.asked = asked  # type: ignore[attr-defined]
    return council


# ── velune_ask ───────────────────────────────────────────────────────────────


async def test_velune_ask_runs_the_council():
    council = _council()
    result = await VeluneMCPServer(council=council)._velune_ask("hello")
    assert result == {
        "response": "council says hi",
        "degraded": False,
        "notes": [],
        "run_id": "run-1",
        "model": "velune-council",
    }
    assert council.asked == ["hello"]


async def test_velune_ask_reports_a_corrected_plan_as_degraded():
    council = _council(_outcome(notes=["Router answer unusable; using the skill's default team."]))
    result = await VeluneMCPServer(council=council)._velune_ask("hello")
    assert result["response"] == "council says hi" and result["degraded"] is True


async def test_velune_ask_reports_a_failed_run_as_an_error():
    council = _council(_outcome(ok=False, error="writer failed: RuntimeError: boom"))
    result = await VeluneMCPServer(council=council)._velune_ask("hello")
    assert result == {"error": "writer failed: RuntimeError: boom"}


async def test_velune_ask_without_a_council_names_the_fix():
    result = await VeluneMCPServer()._velune_ask("hello")
    assert result == {"error": COUNCIL_UNAVAILABLE}
    assert "velune mcp serve" in result["error"]


async def test_velune_ask_turns_an_exception_into_an_error():
    server = VeluneMCPServer(council=_council(error=RuntimeError("boom")))
    assert await server._velune_ask("hello") == {"error": "boom"}


async def test_velune_ask_refuses_a_workspace_outside_the_allowed_ones(tmp_path):
    council = _council()
    server = VeluneMCPServer(workspace_path=tmp_path / "a", council=council)
    result = await server._velune_ask("hello", workspace_path=str(tmp_path / "elsewhere"))
    assert "error" in result and council.asked == []


async def test_json_rpc_velune_ask_reaches_the_council():
    server = VeluneMCPServer(council=_council())
    reply = await server.handle_json_rpc_request(
        {"jsonrpc": "2.0", "id": 7, "method": "velune_ask", "params": {"prompt": "hello"}}
    )
    assert reply["id"] == 7
    assert reply["result"]["response"] == "council says hi"


# ── sampling ─────────────────────────────────────────────────────────────────

PARAMS = {"messages": [{"content": {"text": "first"}}, {"content": {"text": "second"}}]}


async def test_sampling_returns_the_council_answer():
    council = _council()
    reply = await VeluneMCPServer(council=council)._velune_sampling_create_message(PARAMS)
    assert reply["content"]["text"] == "council says hi"
    assert reply["stopReason"] == "endTurn"
    assert council.asked == ["first\nsecond"]


async def test_sampling_reports_failure_as_an_error_stop():
    council = _council(_outcome(ok=False, error="Stopped: the time limit (120s) was reached."))
    reply = await VeluneMCPServer(council=council)._velune_sampling_create_message(PARAMS)
    assert reply["stopReason"] == "error"
    assert reply["content"]["text"] == "Stopped: the time limit (120s) was reached."


async def test_sampling_without_a_council_is_an_error_with_guidance():
    reply = await VeluneMCPServer()._velune_sampling_create_message(PARAMS)
    assert reply["stopReason"] == "error"
    assert reply["content"]["text"] == COUNCIL_UNAVAILABLE


async def test_sampling_turns_an_exception_into_an_error_stop():
    server = VeluneMCPServer(council=_council(error=RuntimeError("boom")))
    reply = await server._velune_sampling_create_message(PARAMS)
    assert reply["stopReason"] == "error" and "boom" in reply["content"]["text"]


# ── `velune mcp serve` ───────────────────────────────────────────────────────


def _serve(monkeypatch, command):
    built = MagicMock()
    monkeypatch.setattr("velune.mcp.server.VeluneMCPServer", built)
    services = {"runtime.tool_registry": "registry"}
    container = SimpleNamespace(get=lambda key: services[key], has=lambda key: key in services)
    monkeypatch.setattr(
        mcp_commands, "build_runtime", lambda *a, **k: SimpleNamespace(container=container)
    )
    submitted: list = []
    monkeypatch.setattr(mcp_commands, "submit", submitted.append)
    command(SimpleNamespace(obj=None))
    return built, submitted, container


@pytest.mark.parametrize("command", [mcp_commands.mcp_serve_subcmd, mcp_commands.mcp_serve])
def test_mcp_serve_gives_the_server_a_council(monkeypatch, command):
    built, submitted, _ = _serve(monkeypatch, command)
    built.assert_called_once()
    assert built.call_args.args == ("registry",)
    assert callable(built.call_args.kwargs["council"])
    assert len(submitted) == 1


async def test_the_mcp_council_is_read_only_and_short(monkeypatch):
    from velune.permissions import ExecutionMode

    seen: dict = {}

    async def fake_run_headless(container, task, **kwargs):
        seen.update(kwargs, container=container, task=task)
        return _outcome()

    monkeypatch.setattr("velune.swarm.setup.run_headless", fake_run_headless)
    container = object()
    await mcp_commands.council_runner(container)("hello")
    assert seen["container"] is container and seen["task"] == "hello"
    assert seen["mode"] is ExecutionMode.PLAN
    assert seen["max_seconds"] == mcp_commands.MCP_COUNCIL_SECONDS
