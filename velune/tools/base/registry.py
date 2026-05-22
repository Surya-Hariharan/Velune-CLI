"""Tool registry."""

from __future__ import annotations

from typing import Optional

from velune.tools.base.tool import BaseTool


class ToolRegistry:
    """Registry for available tools."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool, *, replace: bool = True) -> None:
        """Register a tool."""

        name = tool.get_name()
        if not replace and name in self._tools:
            raise ValueError(f"Tool already registered: {name}")
        self._tools[name] = tool

    def get(self, name: str) -> Optional[BaseTool]:
        """Get a tool by name."""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""

        return name in self._tools

    def unregister(self, name: str) -> None:
        """Remove a tool by name."""

        self._tools.pop(name, None)

    def list_tools(self) -> list[str]:
        """List all registered tool names."""
        return list(self._tools.keys())

    def list_tool_schemas(self) -> list[dict[str, object]]:
        """List schemas and capability metadata for all tools."""

        schemas: list[dict[str, object]] = []
        for tool in self._tools.values():
            schemas.append(
                {
                    "name": tool.get_name(),
                    "description": tool.get_description(),
                    "schema": tool.get_schema(),
                    "permissions": sorted(permission.value for permission in tool.get_required_permissions()),
                }
            )
        return schemas
