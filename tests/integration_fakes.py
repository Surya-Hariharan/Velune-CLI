"""Shared fakes for the integration-discovery tests (VAIR A2).

Everything here is deterministic and offline: a fake application connector, a
scripted model provider that records the exact tool list of every request, and a
builder that wires the *production* ResourceManager, PermissionGate and
ToolLoopRunner together.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from velune.core.types.inference import InferenceRequest, InferenceResponse, ToolCall
from velune.orchestration.tool_loop import ToolLoopRunner
from velune.permissions.boundary import Boundary
from velune.permissions.gate import PermissionGate
from velune.permissions.policy import ExecutionMode, PolicyState
from velune.resources.base import (
    DiscoveryHint,
    ErrorCode,
    Lifecycle,
    ResourceCapability,
    ResourceConnector,
    ResourcePermission,
    ResourceResult,
    ResourceState,
    ResourceStatus,
)
from velune.resources.manager import ResourceManager
from velune.resources.tools import IntegrationToolSource
from velune.tools.base.tool import BaseTool, ToolCallContext, ToolPermission

SECRET = "hunter2-secret-value"


class FakeApp(ResourceConnector):
    """A fake application with one operation of every tier."""

    resource_id = "fakeapp"
    display_name = "Fake App"
    description = "A fake application used in tests."
    keywords = ("fake", "widget")
    lifecycle = Lifecycle.PERSISTENT

    def __init__(
        self,
        name: str | None = None,
        *,
        available: bool = True,
        problem: str | None = None,
        fail_with: ErrorCode | None = None,
    ) -> None:
        super().__init__(name)
        self.available = available
        self.problem = problem
        self.fail_with = fail_with
        self.ran: list[tuple[str, dict[str, Any]]] = []
        self.state = ResourceState.DISCONNECTED
        self.connects = 0
        self.probes = 0

    async def connect(self) -> ResourceResult:
        self.connects += 1
        if not self.available:
            self.state = ResourceState.ERROR
            return ResourceResult.failure(
                "app is down", action="connect", code=ErrorCode.CONNECTION_FAILED
            )
        self.state = ResourceState.CONNECTED
        return ResourceResult.success("connect")

    async def disconnect(self) -> ResourceResult:
        self.state = ResourceState.DISCONNECTED
        return ResourceResult.success("disconnect")

    def status(self) -> ResourceStatus:
        return ResourceStatus(self.resource_id, self.display_name, self.state)

    async def discover(self) -> list[DiscoveryHint]:
        return []

    def configuration_problem(self) -> str | None:
        return self.problem

    async def probe_availability(self) -> tuple[bool, str]:
        self.probes += 1
        return (True, "") if self.available else (False, "the app is not running")

    def capabilities(self) -> list[ResourceCapability]:
        read, write = ResourcePermission.READ, ResourcePermission.WRITE
        key = {"type": "string", "description": "Item key."}
        return [
            ResourceCapability(
                "peek",
                read,
                "Read one item",
                scope="fakeapp.item.read",
                category="inspect",
                params={"properties": {"key": key}, "required": ["key"]},
            ),
            ResourceCapability(
                "count", read, "Count items", scope="fakeapp.item.count", category="inspect"
            ),
            ResourceCapability(
                "poke",
                write,
                "Write one item",
                scope="fakeapp.item.write",
                category="change",
                params={
                    "properties": {
                        "key": key,
                        "value": {"type": "string", "description": "New value."},
                    },
                    "required": ["key", "value"],
                },
            ),
            ResourceCapability(
                "nuke",
                ResourcePermission.ADMIN,
                "Delete everything",
                destructive=True,
                scope="fakeapp.item.delete",
                category="change",
            ),
        ]

    async def execute(self, action: str, params: dict[str, Any] | None = None) -> ResourceResult:
        self.ran.append((action, dict(params or {})))
        if self.fail_with is not None:
            return ResourceResult.failure("the app went away", action=action, code=self.fail_with)
        if action == "peek":
            return ResourceResult.success(
                action,
                {
                    "key": (params or {}).get("key"),
                    "password": SECRET,
                    "note": "ok",
                    "detail": f"authenticated with {SECRET}",
                },
            )
        return ResourceResult.success(action, {"done": action})


def many_ops_app(count: int, *, app_id: str = "bigapp") -> type[ResourceConnector]:
    """A connector with *count* read operations (for tool-budget tests)."""

    class BigApp(FakeApp):
        resource_id = app_id
        display_name = "Big App"
        description = "An application with many operations."
        keywords = ("big",)

        def capabilities(self) -> list[ResourceCapability]:
            return [
                ResourceCapability(
                    f"op{i:02d}",
                    ResourcePermission.READ,
                    f"Operation {i}",
                    scope=f"{app_id}.op.op{i:02d}",
                    category="inspect",
                )
                for i in range(count)
            ]

    return BigApp


class OtherApp(FakeApp):
    resource_id = "otherapp"
    display_name = "Other App"
    description = "A second, unrelated application."
    keywords = ("other",)


class EchoTool(BaseTool):
    """A trivial native tool, so tests can see native + integration tools together."""

    def get_name(self) -> str:
        return "native_echo"

    def get_description(self) -> str:
        return "Echo text"

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.FILESYSTEM_READ}

    def get_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, **kwargs: Any) -> str:
        return f"echo {kwargs.get('text', '')}"


class ScriptedProvider:
    """Returns scripted responses and records the tool names of every request."""

    def __init__(self, *responses: InferenceResponse) -> None:
        self._responses = list(responses)
        self.requests: list[InferenceRequest] = []

    @property
    def tool_names(self) -> list[list[str]]:
        return [[t["function"]["name"] for t in (r.tools or [])] for r in self.requests]

    def get_capabilities(self) -> Any:
        return SimpleNamespace(supports_streaming=False)

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        self.requests.append(request)
        return self._responses.pop(0)


def call(name: str, call_id: str = "c1", **arguments: Any) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def turn(*calls: ToolCall) -> InferenceResponse:
    return InferenceResponse(
        content="",
        model_id="m",
        finish_reason="tool_calls",
        tokens_used=1,
        latency_ms=1.0,
        tool_calls=list(calls),
    )


def final(text: str = "done") -> InferenceResponse:
    return InferenceResponse(
        content=text, model_id="m", finish_reason="stop", tokens_used=1, latency_ms=1.0
    )


async def allow_all(name: str, args: dict[str, Any], permissions: set[Any]) -> bool:
    """The REPL's approver: records the call; the permission gate is what decides."""
    return True


@dataclass
class Env:
    """The production pieces wired together for one test."""

    manager: ResourceManager
    source: IntegrationToolSource
    gate: PermissionGate
    records: list[dict[str, Any]] = field(default_factory=list)
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def runner(
        self,
        provider: ScriptedProvider,
        *,
        registry: Any = None,
        mcp: Any = None,
        approver: Any = allow_all,
        max_turns: int = 10,
    ) -> ToolLoopRunner:
        ctx = ToolCallContext(
            run_id="r", actor="test", workspace=self.gate.boundary.workspace_root, gate=self.gate
        )
        return ToolLoopRunner(
            provider,
            registry,
            mcp_registry=mcp,
            approver=approver,
            ctx=ctx,
            max_turns=max_turns,
            on_event=lambda event, data: self.events.append((event, data)),
            dynamic_tools=self.source,
        )

    async def run(self, provider: ScriptedProvider, task: str = "do the thing", **kw: Any) -> Any:
        self.source.begin_run(task)
        request = InferenceRequest(model_id="m", messages=[{"role": "user", "content": task}])
        return await self.runner(provider, **kw).run(request)


def make_env(
    tmp_path: Path,
    mode: ExecutionMode = ExecutionMode.MANUAL,
    *,
    connectors: dict[str, Any] | None = None,
    ask: Any = None,
    max_tools: int = 24,
    job_registry: Any = None,
) -> Env:
    records: list[dict[str, Any]] = []
    gate = PermissionGate(
        state=PolicyState(mode=mode),
        boundary=Boundary(tmp_path),
        ask=ask,
        audit=records.append,
    )

    def factory() -> ToolCallContext:
        return ToolCallContext(run_id="r", actor="test", workspace=tmp_path, gate=gate)

    manager = ResourceManager(workspace=tmp_path, context_factory=factory)
    for rid, ctor in (connectors if connectors is not None else {"fakeapp": FakeApp}).items():
        manager.register(rid, ctor)
    if job_registry is not None:
        manager.attach_job_registry(job_registry)
    source = IntegrationToolSource(manager, max_tools=max_tools)
    return Env(manager=manager, source=source, gate=gate, records=records)
