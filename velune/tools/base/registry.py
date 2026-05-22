"""Tool registry."""

from typing import Dict, Optional, list
from velune.tools.base.tool import BaseTool


class ToolRegistry:
    """Registry for available tools."""

    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Register a tool."""
        self._tools[tool.get_name()] = tool

    def get(self, name: str) -> Optional[BaseTool]:
        """Get a tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        """List all registered tool names."""
        return list(self._tools.keys())

    def list_tool_schemas(self) -> list[Dict[str, any]]:
        """List schemas for all tools."""
        schemas = []
        for tool in self._tools.values():
            schemas.append({
                "name": tool.get_name(),
                "description": tool.get_description(),
                "schema": tool.get_schema(),
            })
        return schemas
