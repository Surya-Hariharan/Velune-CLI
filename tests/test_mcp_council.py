"""The MCP server must run the real council, or say precisely why it cannot."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, create_autospec

import pytest

from tests.council_fakes import make_orchestrator
from velune.cli.commands import mcp as mcp_commands
from velune.cognition.orchestrator import CouncilOrchestrator
from velune.mcp.server import COUNCIL_UNAVAILABLE, VeluneMCPServer

RESULT = {
    "final_summary": "council says hi",
    "arbitration": {"overall_confidence": 0.8, "flags": []},
    "degraded": False,
}


def _stub(result=None, *, error=None):
    """An autospec'd orchestrator: calling anything it does not really have (e.g. the
    old ``.run``) raises AttributeError, and execute_task is signature-checked."""
    stub = create_autospec(CouncilOrchestrator, instance=True)
    if error is not None:
        stub.execute_task.side_effect = error
    else:
        stub.execute_task.return_value = RESULT if result is None else result
    return stub


def test_the_orchestrator_double_really_lacks_run():
    assert not hasattr(_stub(), "run")


# ── velune_ask ───────────────────────────────────────────────────────────────


async def test_velune_ask_runs_the_real_council_api():
    stub = _stub()
    server = VeluneMCPServer(council_orchestrator=stub)
    result = await server._velune_ask("hello")
    assert result == {
        "response": "council says hi",
        "confidence": 0.8,
        "degraded": False,
        "model": "velune-council",
    }
    stub.execute_task.assert_awaited_once()
    args, kwargs = stub.execute_task.await_args
    assert args[0] == "hello" and args[1].startswith("Repository: ")
    assert kwargs["budget"].max_wall_time_seconds == 30
    assert kwargs["budget"].max_review_cycles == 1


async def test_velune_ask_reports_a_degraded_answer():
    degraded = {**RESULT, "degraded": True}
    result = await VeluneMCPServer(council_orchestrator=_stub(degraded))._velune_ask("hello")
    assert result["response"] == "council says hi" and result["degraded"] is True


async def test_velune_ask_reports_a_timeout_as_an_error(monkeypatch):
    orch, _ = make_orchestrator(monkeypatch)
    timed_out = orch._build_timeout_result("p")
    result = await VeluneMCPServer(council_orchestrator=_stub(timed_out))._velune_ask("hello")
    assert "timed out" in result["error"]
    assert "response" not in result


async def test_velune_ask_reports_total_agent_failure_as_an_error(monkeypatch):
    orch, _ = make_orchestrator(monkeypatch)
    failed = orch._build_timeout_result(
        "p", flag="ALL_AGENTS_FAILED", summary="The council could not produce an answer."
    )
    result = await VeluneMCPServer(council_orchestrator=_stub(failed))._velune_ask("hello")
    assert result == {"error": "The council could not produce an answer."}


async def test_velune_ask_without_an_orchestrator_names_the_fix():
    result = await VeluneMCPServer()._velune_ask("hello")
    assert result == {"error": COUNCIL_UNAVAILABLE}
    assert "velune mcp serve" in result["error"]


async def test_velune_ask_turns_an_orchestrator_exception_into_an_error():
    server = VeluneMCPServer(council_orchestrator=_stub(error=RuntimeError("boom")))
    assert await server._velune_ask("hello") == {"error": "boom"}


async def test_json_rpc_velune_ask_reaches_the_council():
    server = VeluneMCPServer(council_orchestrator=_stub())
    reply = await server.handle_json_rpc_request(
        {"jsonrpc": "2.0", "id": 7, "method": "velune_ask", "params": {"prompt": "hello"}}
    )
    assert reply["id"] == 7
    assert reply["result"]["response"] == "council says hi"


# ── sampling ─────────────────────────────────────────────────────────────────

PARAMS = {"messages": [{"content": {"text": "first"}}, {"content": {"text": "second"}}]}


async def test_sampling_returns_the_council_answer():
    stub = _stub()
    reply = await VeluneMCPServer(council_orchestrator=stub)._velune_sampling_create_message(PARAMS)
    assert reply["content"]["text"] == "council says hi"
    assert reply["stopReason"] == "endTurn"
    assert stub.execute_task.await_args.args[0] == "first\nsecond"


async def test_sampling_reports_failure_as_an_error_stop():
    timed_out = {"final_summary": "ran out of time", "is_timeout": True, "arbitration": {}}
    reply = await VeluneMCPServer(
        council_orchestrator=_stub(timed_out)
    )._velune_sampling_create_message(PARAMS)
    assert reply["stopReason"] == "error"
    assert reply["content"]["text"] == "ran out of time"


async def test_sampling_without_an_orchestrator_is_an_error_with_guidance():
    reply = await VeluneMCPServer()._velune_sampling_create_message(PARAMS)
    assert reply["stopReason"] == "error"
    assert reply["content"]["text"] == COUNCIL_UNAVAILABLE


async def test_sampling_turns_an_exception_into_an_error_stop():
    server = VeluneMCPServer(council_orchestrator=_stub(error=RuntimeError("boom")))
    reply = await server._velune_sampling_create_message(PARAMS)
    assert reply["stopReason"] == "error" and "boom" in reply["content"]["text"]


# ── construction and `velune mcp serve` ──────────────────────────────────────


def test_the_server_keeps_the_orchestrator_it_is_given():
    stub = _stub()
    assert VeluneMCPServer(council_orchestrator=stub).council_orchestrator is stub
    assert VeluneMCPServer().council_orchestrator is None


def _serve(monkeypatch, services, command):
    built = MagicMock()
    monkeypatch.setattr("velune.mcp.server.VeluneMCPServer", built)
    container = SimpleNamespace(get=lambda key: services[key], has=lambda key: key in services)
    monkeypatch.setattr(
        mcp_commands, "build_runtime", lambda *a, **k: SimpleNamespace(container=container)
    )
    submitted: list = []
    monkeypatch.setattr(mcp_commands, "submit", submitted.append)
    command(SimpleNamespace(obj=None))
    return built, submitted


@pytest.mark.parametrize("command", [mcp_commands.mcp_serve_subcmd, mcp_commands.mcp_serve])
def test_mcp_serve_hands_the_runtime_orchestrator_to_the_server(monkeypatch, command):
    stub = _stub()
    built, submitted = _serve(
        monkeypatch,
        {"runtime.tool_registry": "registry", "runtime.council_orchestrator": stub},
        command,
    )
    built.assert_called_once()
    assert built.call_args.args == ("registry",)
    assert built.call_args.kwargs["council_orchestrator"] is stub
    assert len(submitted) == 1


def test_mcp_serve_without_a_runtime_orchestrator_passes_none(monkeypatch):
    built, _ = _serve(
        monkeypatch, {"runtime.tool_registry": "registry"}, mcp_commands.mcp_serve_subcmd
    )
    assert built.call_args.kwargs["council_orchestrator"] is None
