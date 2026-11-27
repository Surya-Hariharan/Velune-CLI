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
        self.function_calling = True

    def get_capabilities(self) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(
            supports_function_calling=self.function_calling, supports_streaming=False
        )

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
            if isinstance(answer, BaseException):  # CancelledError too
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


def model(model_id: str = "fake-model", *, local: bool = False) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        model_id=model_id,
        provider_id="fake",
        is_local=local,
        context_length=32_768,
        metadata={},
    )


def descriptor(model_id: str = "m1", provider_id: str = "fake", *, is_local: bool = True) -> Any:
    """A real ``ModelDescriptor`` (for code that validates it)."""
    from velune.core.types.model import ModelCapabilityProfile, ModelDescriptor

    return ModelDescriptor(
        model_id=model_id,
        provider_id=provider_id,
        display_name=model_id,
        context_length=32768,
        capabilities=ModelCapabilityProfile(),
        is_local=is_local,
    )


class FakeContainer:
    def __init__(self, services: dict[str, Any]) -> None:
        self.services = services
        self.asked: list[str] = []

    def get(self, key: str) -> Any:
        self.asked.append(key)
        if key not in self.services:
            raise KeyError(key)
        return self.services[key]

    def get_optional(self, key: str) -> Any:
        return self.services.get(key)

    def has(self, key: str) -> bool:
        return key in self.services


def fake_repl(
    workspace: Path,
    provider: Any,
    *,
    tools: ToolRegistry | None = None,
    cancelled: bool = False,
    mode: Any = None,
) -> Any:
    """Just enough of ``VeluneREPL`` for ``/council``; ``repl.output`` holds what it printed."""
    import contextlib
    from io import StringIO
    from types import SimpleNamespace

    from rich.console import Console

    from velune.permissions import ExecutionMode

    buffer = StringIO()

    @contextlib.asynccontextmanager
    async def foreground():
        yield

    the_model = model()

    async def resolve():
        return the_model, provider

    container = FakeContainer(
        {"runtime.workspace": str(workspace), "runtime.tool_registry": tools or ToolRegistry()}
    )
    repl = SimpleNamespace(
        console=Console(file=buffer, width=120, color_system=None),
        container=container,
        runtime=SimpleNamespace(config=None),
        output=buffer,
        active_model=the_model,
        _resolve_active_model_and_provider=resolve,
        _interrupts=SimpleNamespace(
            foreground=foreground, consume_user_cancelled=lambda: cancelled
        ),
        _conversation=[],
        _last_turn_journal=None,
        _session_id="s1",
        _episodic_session_id=None,
        _hook_dispatcher=None,
        _mcp_registry=None,
        _tool_session_grants=set(),
        _tools_unsupported_models=set(),
        _execution_mode=mode or ExecutionMode.MANUAL,
        _plugin_manager=None,
        _job_registry=None,
        _hunk_review_mode=False,
    )
    return repl


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
