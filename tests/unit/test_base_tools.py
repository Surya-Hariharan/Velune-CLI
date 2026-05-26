"""Unit tests for Velune base tool registry and execution coordinator (Batch 13)."""

import pytest
import asyncio
from typing import Any

from velune.tools.base.tool import BaseTool, ToolPermission
from velune.tools.base.registry import ToolRegistry
from velune.tools.base.executor import ToolExecutionCoordinator, ToolExecutionResult


class DummyAddTool(BaseTool):
    """Dummy tool for mathematical addition to test tool base framework."""
    
    def __init__(self, requires_perms: set[ToolPermission] | None = None) -> None:
        self.requires_perms = requires_perms or set()
        self.input_validated = False

    def get_name(self) -> str:
        return "add_tool"

    def get_description(self) -> str:
        return "Adds two integers."

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "integer"}
            },
            "required": ["a", "b"]
        }

    def get_required_permissions(self) -> set[ToolPermission]:
        return self.requires_perms

    def validate_input(self, payload: dict[str, Any]) -> None:
        self.input_validated = True
        if not isinstance(payload.get("a"), int) or not isinstance(payload.get("b"), int):
            raise ValueError("Parameters 'a' and 'b' must be integers.")

    async def execute(self, **kwargs) -> Any:
        # Simulate slight delay to support timeout test cases
        if kwargs.get("delay", 0.0) > 0:
            await asyncio.sleep(kwargs["delay"])
        return kwargs["a"] + kwargs["b"]


class BrokenTool(BaseTool):
    """Tool that fails validation during registration."""
    
    def get_name(self) -> str:
        raise RuntimeError("Broken name lookup")

    def get_description(self) -> str:
        return "Broken description"

    async def execute(self, **kwargs) -> Any:
        pass


def test_tool_registry_registration_and_validation() -> None:
    """Verify that ToolRegistry registers valid tools and rejects invalid tools."""
    registry = ToolRegistry()
    tool = DummyAddTool()
    
    # 1. Validation and registration
    assert registry.validate_tool(tool) is True
    registry.register(tool)
    assert registry.has("add_tool") is True
    assert registry.get("add_tool") == tool
    
    # 2. Re-registering with replace=False raises ValueError
    with pytest.raises(ValueError, match="Tool already registered"):
        registry.register(tool, replace=False)
        
    # 3. Listing and schemas
    assert "add_tool" in registry.list_tools()
    schemas = registry.list_tool_schemas()
    assert len(schemas) == 1
    assert schemas[0]["name"] == "add_tool"
    assert schemas[0]["description"] == "Adds two integers."
    
    # 4. Broken tool handling
    broken = BrokenTool()
    assert registry.validate_tool(broken) is False
    registry.register(broken)
    assert "BrokenTool" in registry.list_broken_tools()
    
    # 5. Unregister
    registry.unregister("add_tool")
    assert registry.has("add_tool") is False


@pytest.mark.asyncio
async def test_tool_executor_happy_path() -> None:
    """Verify that the ToolExecutionCoordinator executes registered tools successfully."""
    registry = ToolRegistry()
    tool = DummyAddTool()
    registry.register(tool)
    
    executor = ToolExecutionCoordinator(registry)
    
    result = await executor.execute(
        tool_name="add_tool",
        arguments={"a": 10, "b": 20},
        run_id="run-1",
        actor="user"
    )
    
    assert result.success is True
    assert result.output == 30
    assert result.attempts == 1
    assert result.error is None
    assert tool.input_validated is True


@pytest.mark.asyncio
async def test_tool_executor_missing_permissions() -> None:
    """Verify that the ToolExecutionCoordinator blocks execution on missing permissions."""
    registry = ToolRegistry()
    tool = DummyAddTool(requires_perms={ToolPermission.TERMINAL_EXECUTE})
    registry.register(tool)
    
    executor = ToolExecutionCoordinator(registry)
    
    # Execute with permissions missing TERMINAL_EXECUTE
    result = await executor.execute(
        tool_name="add_tool",
        arguments={"a": 1, "b": 2},
        run_id="run-2",
        actor="user",
        granted_permissions={ToolPermission.FILESYSTEM_READ}
    )
    
    assert result.success is False
    assert "missing_permissions" in result.error
    assert ToolPermission.TERMINAL_EXECUTE.value in result.error


@pytest.mark.asyncio
async def test_tool_executor_tool_not_found() -> None:
    """Verify that the ToolExecutionCoordinator returns an error for unregistered tools."""
    registry = ToolRegistry()
    executor = ToolExecutionCoordinator(registry)
    
    result = await executor.execute(
        tool_name="non_existent",
        arguments={},
        run_id="run-3",
        actor="user"
    )
    
    assert result.success is False
    assert result.error == "tool_not_found"


@pytest.mark.asyncio
async def test_tool_executor_timeout_and_retries() -> None:
    """Verify that the ToolExecutionCoordinator enforces timeouts and executes retries on failure."""
    registry = ToolRegistry()
    tool = DummyAddTool()
    registry.register(tool)
    
    executor = ToolExecutionCoordinator(registry, default_timeout=0.05, default_retries=2)
    
    # Trigger a timeout by setting a delay greater than the timeout budget
    result = await executor.execute(
        tool_name="add_tool",
        arguments={"a": 5, "b": 5, "delay": 0.2},
        run_id="run-4",
        actor="user"
    )
    
    assert result.success is False
    # Max attempts: default_retries (2) + 1 = 3
    assert result.attempts == 3
