"""Fakes for swarm tests: a scripted provider, tools, a recording gate."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from velune.core.types.inference import InferenceRequest, InferenceResponse, ToolCall
from velune.permissions.boundary import Boundary
from velune.tools.base.registry import ToolRegistry
from velune.tools.base.tool import BaseTool, ToolPermission


def text(content: str, tokens: int = 10) -> InferenceResponse:
    return InferenceResponse(
        content=content, model_id="fake", finish_reason="stop", tokens_used=tokens, latency_ms=1.0
    )


def calls(*items: tuple[str, dict[str, Any]]) -> InferenceResponse:
    return InferenceResponse(
        content="",
        model_id="fake",
        finish_reason="tool_calls",
        tokens_used=10,
        latency_ms=1.0,
        tool_calls=[ToolCall(id=f"c{i}", name=n, arguments=a) for i, (n, a) in enumerate(items)],
    )


class ScriptedProvider:
    """Answers each request with ``script(request)``; records every request.

    *script* may be a list (answers popped in order) or a function of the
    request, which lets concurrent agents be answered by who is asking.
    """

    provider_id = "fake"

    def __init__(
        self,
        script: list[InferenceResponse] | Callable[[InferenceRequest], Any],
        *,
        delay: float = 0.0,
    ) -> None:
        self._script = script
        self.delay = delay
        self.requests: list[InferenceRequest] = []
        self.active = 0
        self.max_active = 0

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        self.requests.append(request)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if callable(self._script):
                answer = self._script(request)
            else:
                if not self._script:
                    raise AssertionError("ScriptedProvider ran out of answers")
                answer = self._script.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        finally:
            self.active -= 1


def system_of(request: InferenceRequest) -> str:
    return request.messages[0]["content"]


def user_of(request: InferenceRequest) -> str:
    return request.messages[1]["content"]


class ReadTool(BaseTool):
    def get_name(self) -> str:
        return "read_file"

    def get_description(self) -> str:
        return "Read a file"

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.FILESYSTEM_READ}

    def get_schema(self) -> dict:
        return {"type": "object", "properties": {"file_path": {"type": "string"}}}

    async def execute(self, file_path: str = "", **_: Any) -> str:
        return f"contents of {file_path}"


class WriteTool(BaseTool):
    """Writes into a dict instead of the disk; declares a real file change."""

    def __init__(self) -> None:
        self.written: dict[str, str] = {}

    def get_name(self) -> str:
        return "write_file"

    def get_description(self) -> str:
        return "Write a file"

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.FILESYSTEM_WRITE}

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}},
        }

    async def execute(self, file_path: str = "", content: str = "", **_: Any) -> str:
        self.written[file_path] = content
        return "written"


class ShellTool(BaseTool):
    def get_name(self) -> str:
        return "execute_command"

    def get_description(self) -> str:
        return "Run a command"

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.TERMINAL_EXECUTE}

    def get_schema(self) -> dict:
        return {"type": "object", "properties": {"command": {"type": "string"}}}

    async def execute(self, **_: Any) -> str:
        return "ran"


def registry(*tools: BaseTool) -> ToolRegistry:
    reg = ToolRegistry()
    for tool in tools:
        reg.register(tool)
    return reg


class RecordingGate:
    """Stands in for the session's PermissionGate: allows and records each check."""

    def __init__(self, workspace: Path) -> None:
        self.boundary = Boundary(workspace)
        self.checked: list[str] = []

    async def check(self, tool_name: str, actions: list[Any]) -> list[Path]:
        self.checked.append(tool_name)
        return []

    async def preauthorize(self, calls: list[Any]) -> None:
        return None

    def clear_batch(self) -> None:
        return None
