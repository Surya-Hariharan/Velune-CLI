"""MCP client consuming external MCP servers."""

from __future__ import annotations

from typing import Any, Optional
from mcp.types import Tool
from velune.tools.base.tool import BaseTool, ToolPermission


class MCPToolWrapper(BaseTool):
    """Wraps an external MCP tool as a Velune BaseTool."""

    def __init__(self, client: VeluneMCPClient, name: str, description: str, input_schema: dict):
        self.client = client
        self.name = name
        self.description = description
        self.input_schema = input_schema

    def get_name(self) -> str:
        # Namespace tool with the server name prefix to avoid collisions
        return f"{self.client.server_name}_{self.name}"

    def get_description(self) -> str:
        return self.description

    def get_schema(self) -> dict[str, Any]:
        return self.input_schema

    async def execute(self, **kwargs) -> Any:
        if not self.client.session:
            raise RuntimeError(f"Client for tool {self.get_name()} is not connected.")
        result = await self.client.session.call_tool(self.name, arguments=kwargs)
        
        # CallToolResult structure holds content. Let's parse text contents.
        text_contents = []
        if hasattr(result, "content") and result.content:
            for content in result.content:
                if hasattr(content, "text"):
                    text_contents.append(content.text)
                elif isinstance(content, dict) and "text" in content:
                    text_contents.append(content["text"])
                else:
                    text_contents.append(str(content))
        return "\n".join(text_contents) if text_contents else str(result)


class VeluneMCPClient:
    """Connects to external MCP servers and exposes them as Velune tools."""

    def __init__(self, server_url: str, server_name: str):
        self.server_url = server_url
        self.server_name = server_name
        self._sse_ctx = None
        self._session_ctx = None
        self.session = None
        self.raw_tools: list[Tool] = []

    async def connect(self) -> list[dict]:
        """Connect and return available tools."""
        from mcp.client.sse import sse_client
        from mcp import ClientSession

        self._sse_ctx = sse_client(self.server_url)
        self._read, self._write = await self._sse_ctx.__aenter__()

        self._session_ctx = ClientSession(self._read, self._write)
        self.session = await self._session_ctx.__aenter__()
        await self.session.initialize()

        tools_result = await self.session.list_tools()
        self.raw_tools = tools_result.tools

        # Convert tools to list of dict
        result = []
        for tool in self.raw_tools:
            result.append({
                "name": tool.name,
                "description": tool.description or "",
                "inputSchema": tool.inputSchema,
            })
        return result

    def to_velune_tools(self) -> list[BaseTool]:
        """Convert MCP tools to Velune BaseTool wrappers."""
        velune_tools = []
        for tool in self.raw_tools:
            velune_tools.append(
                MCPToolWrapper(
                    client=self,
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=tool.inputSchema,
                )
            )
        return velune_tools

    async def disconnect(self) -> None:
        """Disconnect and clean up resources."""
        if self._session_ctx:
            try:
                await self._session_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self._session_ctx = None
            self.session = None
        if self._sse_ctx:
            try:
                await self._sse_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self._sse_ctx = None
