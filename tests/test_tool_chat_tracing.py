"""Per-tool-call trace events must reach the CognitiveBus from the default chat path.

Previously ``tool_chat.py``'s native tool loop never called ``bus.emit`` at
all — ``velune trace`` could only show one 200-char-truncated
``turn.completed`` per turn, with zero visibility into which tools ran,
unlike a full council/sandbox run. ``_ToolActivityUI`` now mirrors
``tool.started``/``tool.completed``/``tool.denied`` events onto the bus
whenever one is registered, and stays a no-op (not a crash) when it isn't.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from rich.console import Console

from velune.cli.handlers.tool_chat import _ToolActivityUI
from velune.cli.interrupts import InterruptController


class FakeContainer:
    def __init__(self, services: dict[str, Any]) -> None:
        self._services = services

    def get(self, key: str) -> Any:
        if key not in self._services:
            raise KeyError(key)
        return self._services[key]


class FakeBus:
    def __init__(self) -> None:
        self.emitted: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.emitted.append(event)


def _repl(tmp_path: Path, *, bus: Any | None = None):
    console = Console(file=io.StringIO(), force_terminal=False, width=100)
    services: dict[str, Any] = {"runtime.workspace": str(tmp_path)}
    if bus is not None:
        services["runtime.bus"] = bus
    return SimpleNamespace(
        console=console,
        container=FakeContainer(services),
        _fullscreen_ui=None,
        _session_id="testsess",
        _interrupts=InterruptController(),
    )


async def test_tool_start_end_denied_emit_bus_events(tmp_path: Path) -> None:
    bus = FakeBus()
    ui = _ToolActivityUI(_repl(tmp_path, bus=bus))

    ui._on_tool_start({"name": "read_file", "id": "call-1", "arguments": {"file_path": "a.py"}})
    ui._on_tool_end(
        {"name": "read_file", "id": "call-1", "result": "print(1)", "duration_ms": 12.5}
    )
    ui._on_tool_denied({"name": "execute_command", "id": "call-2"})
    await asyncio.sleep(0)  # let the fire-and-forget tasks run

    types_seen = [e.event_type for e in bus.emitted]
    assert types_seen == ["tool.started", "tool.completed", "tool.denied"]

    started = bus.emitted[0]
    assert started.correlation_id == "call-1"
    assert started.data["name"] == "read_file"
    assert started.data["run_id"] == "testsess"

    completed = bus.emitted[1]
    assert completed.data["error"] is False
    assert completed.data["duration_ms"] == 12.5

    denied = bus.emitted[2]
    assert denied.correlation_id == "call-2"
    assert denied.data["name"] == "execute_command"


async def test_tool_events_are_a_no_op_without_a_registered_bus(tmp_path: Path) -> None:
    """No 'runtime.bus' registered (e.g. Tier-1 still warming) must not raise."""
    ui = _ToolActivityUI(_repl(tmp_path, bus=None))

    ui._on_tool_start({"name": "read_file", "id": "call-1", "arguments": {}})
    ui._on_tool_end({"name": "read_file", "id": "call-1", "result": "ok"})
    ui._on_tool_denied({"name": "execute_command", "id": "call-2"})
    await asyncio.sleep(0)
    # No assertion beyond "did not raise" — this is the point of the test.
