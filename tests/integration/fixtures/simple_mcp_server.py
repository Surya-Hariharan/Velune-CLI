"""Minimal MCP stdio server used by integration tests.

Exposes one tool: add(a: int, b: int) -> int.

Run directly: python simple_mcp_server.py
"""

import asyncio

import mcp.server.stdio
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.types import TextContent, Tool

server = Server("simple-test-server")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="add",
            description="Add two integers",
            inputSchema={
                "type": "object",
                "properties": {
                    "a": {"type": "integer"},
                    "b": {"type": "integer"},
                },
                "required": ["a", "b"],
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "add":
        result = arguments["a"] + arguments["b"]
        return [TextContent(type="text", text=str(result))]
    raise ValueError(f"Unknown tool: {name}")


async def main() -> None:
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="simple-test-server",
                server_version="0.1.0",
                capabilities={"tools": {}},
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
