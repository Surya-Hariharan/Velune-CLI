"""Unit tests for Velune base tool registry."""

import asyncio
from typing import Any

import pytest

from velune.tools.base.registry import ToolRegistry
from velune.tools.base.tool import BaseTool, ToolPermission


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
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
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
