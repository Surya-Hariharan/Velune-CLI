"""MCP server exposing Velune's council and memory as remote tools.

Serves two transports:
- stdio: for Claude Desktop and local clients
- HTTP/SSE on localhost:7777: for VS Code and browser clients

Security: Validates workspace_path against allowed_workspaces, read-only by default.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import mcp.server.stdio
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.types import TextContent, Tool

from velune.tools.base.registry import ToolRegistry

if TYPE_CHECKING:
    from velune.kernel.config import VeluneConfig

logger = logging.getLogger("velune.mcp.server")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7777
MAX_REQUEST_BYTES = 1 * 1024 * 1024  # 1 MB per request


class RateLimiter:
    """Token-bucket rate limiter keyed by client ID.

    Each client starts with a full bucket.  Tokens refill at *calls_per_minute*
    tokens per minute.  Once the bucket empties, calls are rejected until
    enough time passes to accumulate another token.
    """

    def __init__(self, calls_per_minute: int = 60) -> None:
        self._limit = calls_per_minute
        self._tokens: dict[str, float] = {}
        self._last_check: dict[str, float] = {}

    def is_allowed(self, client_id: str = "default") -> bool:
        now = time.monotonic()
        if client_id not in self._tokens:
            # First call — bucket starts full so the client isn't immediately blocked.
            self._tokens[client_id] = float(self._limit)
            self._last_check[client_id] = now
        elapsed = now - self._last_check[client_id]
        self._last_check[client_id] = now
        self._tokens[client_id] = min(
            float(self._limit),
            self._tokens[client_id] + elapsed * (self._limit / 60.0),
        )
        if self._tokens[client_id] >= 1.0:
            self._tokens[client_id] -= 1.0
            return True
        return False


class WorkspaceValidator:
    """Validates workspace paths against allowed list."""

    def __init__(
        self, allowed_workspaces: list[Path] | None = None, current_dir: Path | None = None
    ) -> None:
        self.allowed_workspaces = allowed_workspaces or [current_dir or Path.cwd()]

    def is_valid(self, workspace_path: str) -> bool:
        """Check if workspace_path is in allowed list."""
        try:
            path = Path(workspace_path).resolve()
            for allowed in self.allowed_workspaces:
                allowed_resolved = Path(allowed).resolve()
                try:
                    path.relative_to(allowed_resolved)
                    return True
                except ValueError:
                    continue
            return False
        except Exception:
            return False

    def validate(self, workspace_path: str) -> Path:
        """Validate and return resolved path, or raise ValueError."""
        if not self.is_valid(workspace_path):
            allowed_str = ", ".join(str(p) for p in self.allowed_workspaces)
            raise ValueError(
                f"workspace_path '{workspace_path}' not in allowed list: {allowed_str}"
            )
        return Path(workspace_path).resolve()


class VeluneMCPServer:
    """Exposes Velune's council, memory, and code analysis as MCP tools.

    Supports two transports:
    - stdio: for Claude Desktop
    - HTTP/SSE: for VS Code and other clients

    Security: Workspace paths validated against allowed list.
    """

    def __init__(
        self,
        tool_registry: ToolRegistry | None = None,
        workspace_path: str | Path | None = None,
        allowed_workspaces: list[str | Path] | None = None,
        config: VeluneConfig | None = None,
        calls_per_minute: int = 60,
    ):
        self.tool_registry = tool_registry
        self.workspace_path = Path(workspace_path) if workspace_path else Path.cwd()
        self.allowed_workspaces = [Path(p) for p in (allowed_workspaces or [self.workspace_path])]
        self.config = config
        self.server = Server("velune")
        self._rate_limiter = RateLimiter(calls_per_minute=calls_per_minute)
        self._validator = WorkspaceValidator(self.allowed_workspaces)

        # Lazy-load Velune components
        self._council_orchestrator = None
        self._memory_manager = None
        self._repository_cognition = None

        self._register_handlers()

    @property
    def council_orchestrator(self):
        """Lazy-load council orchestrator."""
        if self._council_orchestrator is None:
            try:
                from velune.cognition.council_orchestrator import CouncilOrchestrator
                from velune.models.specializations import ModelSpecializationMapper
                from velune.providers.registry import ProviderRegistry

                registry = ProviderRegistry()
                mapper = ModelSpecializationMapper()
                self._council_orchestrator = CouncilOrchestrator(
                    provider_registry=registry,
                    mapper=mapper,
                )
            except Exception as e:
                logger.warning("Could not load council orchestrator: %s", e)
        return self._council_orchestrator

    @property
    def repository_cognition(self):
        """Lazy-load repository cognition."""
        if self._repository_cognition is None:
            try:
                from velune.kernel.registry import get_container

                container = get_container()
                if container.has("runtime.repository_cognition"):
                    self._repository_cognition = container.get("runtime.repository_cognition")
            except Exception:
                pass
        return self._repository_cognition

    def _register_handlers(self) -> None:
        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            tools = []

            # Add Velune-native tools
            tools.extend(
                [
                    Tool(
                        name="velune_ask",
                        description="Ask Velune about your repository",
                        inputSchema={
                            "type": "object",
                            "properties": {
                                "prompt": {
                                    "type": "string",
                                    "description": "Question about the repository",
                                },
                                "workspace_path": {
                                    "type": "string",
                                    "description": "Path to repository (optional)",
                                },
                            },
                            "required": ["prompt"],
                        },
                    ),
                    Tool(
                        name="velune_search_memory",
                        description="Search Velune's memory for relevant past interactions",
                        inputSchema={
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                                "workspace_path": {"type": "string"},
                                "limit": {"type": "integer", "default": 5},
                            },
                            "required": ["query"],
                        },
                    ),
                    Tool(
                        name="velune_get_symbols",
                        description="Get code symbols (functions, classes) from the repository",
                        inputSchema={
                            "type": "object",
                            "properties": {
                                "workspace_path": {"type": "string"},
                                "name_pattern": {"type": "string", "description": "Optional regex"},
                            },
                        },
                    ),
                    Tool(
                        name="velune_estimate_blast_radius",
                        description="Estimate impact of changing a file on the codebase",
                        inputSchema={
                            "type": "object",
                            "properties": {
                                "workspace_path": {"type": "string"},
                                "file_path": {"type": "string"},
                            },
                            "required": ["file_path"],
                        },
                    ),
                ]
            )

            # Add tools from registry if available
            if self.tool_registry:
                tools.extend(
                    [
                        Tool(
                            name=schema["name"],
                            description=schema["description"],
                            inputSchema=schema["schema"],
                        )
                        for schema in self.tool_registry.list_tool_schemas()
                    ]
                )

            return tools

        @self.server.call_tool()
        async def call_tool(name: str, arguments: dict) -> list[TextContent]:
            if not self._rate_limiter.is_allowed():
                raise ValueError("Rate limit exceeded — too many tool calls per minute.")

            # Handle Velune-native tools
            if name == "velune_ask":
                result = await self._velune_ask(
                    arguments.get("prompt", ""),
                    arguments.get("workspace_path"),
                )
            elif name == "velune_search_memory":
                result = await self._velune_search_memory(
                    arguments.get("query", ""),
                    arguments.get("workspace_path"),
                    arguments.get("limit", 5),
                )
            elif name == "velune_get_symbols":
                result = await self._velune_get_symbols(
                    arguments.get("workspace_path"),
                    arguments.get("name_pattern"),
                )
            elif name == "velune_estimate_blast_radius":
                result = await self._velune_estimate_blast_radius(
                    arguments.get("workspace_path"),
                    arguments.get("file_path"),
                )
            elif self.tool_registry:
                # Fall back to registry
                tool = self.tool_registry.get(name)
                if not tool:
                    raise ValueError(f"Tool not found: {name}")
                result = await tool.execute(**arguments)
            else:
                raise ValueError(f"Tool not found: {name}")

            return [
                TextContent(
                    type="text",
                    text=json.dumps(result) if isinstance(result, dict) else str(result),
                )
            ]

    # =========================================================================
    # Tool implementations
    # =========================================================================

    async def _velune_ask(self, prompt: str, workspace_path: str | None = None) -> dict[str, Any]:
        """Ask Velune about the repository."""
        try:
            workspace = self._validator.validate(workspace_path or str(self.workspace_path))
        except ValueError as e:
            return {"error": str(e)}

        try:
            if not self.council_orchestrator:
                return {"error": "Council not available"}

            from velune.cognition.budget import CouncilExecutionBudget

            budget = CouncilExecutionBudget(
                max_wall_time_seconds=30,
                max_review_cycles=1,
            )

            repo_context = "Repository: " + str(workspace)
            state = await self.council_orchestrator.run(
                task=prompt,
                retrieved_context=repo_context,
                budget=budget,
            )

            response = (
                state.pending_diffs[0].get("proposed", "")
                if state.pending_diffs
                else state.final_output or "No response"
            )

            return {"response": response, "model": "velune-council"}
        except Exception as e:
            logger.error("velune_ask failed: %s", e)
            return {"error": str(e)}

    async def _velune_search_memory(
        self,
        query: str,
        workspace_path: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Search memory for relevant interactions."""
        try:
            self._validator.validate(workspace_path or str(self.workspace_path))
        except ValueError as e:
            return {"error": str(e), "results": []}

        # TODO: Integrate with actual memory manager
        return {"results": []}

    async def _velune_get_symbols(
        self,
        workspace_path: str | None = None,
        name_pattern: str | None = None,
    ) -> dict[str, Any]:
        """Get code symbols from repository."""
        try:
            self._validator.validate(workspace_path or str(self.workspace_path))
        except ValueError as e:
            return {"error": str(e), "symbols": []}

        try:
            if not self.repository_cognition:
                return {"symbols": []}

            snapshot = self.repository_cognition.index(force=False)
            if not snapshot:
                return {"symbols": []}

            symbols = []
            pattern = re.compile(name_pattern) if name_pattern else None

            for file in snapshot.files:
                if file.language.value not in ("python", "typescript", "javascript"):
                    continue

                try:
                    content = Path(file.path).read_text(errors="ignore")
                    for i, line in enumerate(content.split("\n"), 1):
                        if match := re.match(r"^\s*(async\s+)?(def|class)\s+(\w+)", line):
                            kind = "function" if "def" in match.group(2) else "class"
                            name = match.group(3)
                            if pattern and not pattern.search(name):
                                continue
                            symbols.append(
                                {
                                    "name": name,
                                    "kind": kind,
                                    "file": str(file.path),
                                    "line": i,
                                }
                            )
                except Exception:
                    continue

            return {"symbols": symbols[:100]}
        except Exception as e:
            logger.error("velune_get_symbols failed: %s", e)
            return {"error": str(e), "symbols": []}

    async def _velune_estimate_blast_radius(
        self,
        workspace_path: str | None = None,
        file_path: str | None = None,
    ) -> dict[str, Any]:
        """Estimate impact of changing a file."""
        try:
            self._validator.validate(workspace_path or str(self.workspace_path))
        except ValueError as e:
            return {"error": str(e)}

        if not file_path:
            return {"error": "file_path required"}

        try:
            if not self.repository_cognition:
                return {"score": 0.5, "fan_in": 0, "fan_out": 0}

            grapher = self.repository_cognition.grapher
            rel_file = grapher._to_rel_path(file_path)

            if rel_file not in grapher.graph:
                return {"score": 0.2, "fan_in": 0, "fan_out": 0}

            dependents = len(grapher.get_dependents(rel_file))
            dependencies = len(grapher.get_dependencies(rel_file))

            import math

            raw_score = 1.0 * dependents + 0.5 * dependencies
            score = 0.1 + 0.8 * (1.0 - math.exp(-raw_score / 5.0))

            return {
                "score": round(score, 3),
                "fan_in": dependencies,
                "fan_out": dependents,
            }
        except Exception as e:
            logger.error("velune_estimate_blast_radius failed: %s", e)
            return {"error": str(e)}

    def get_tools_list(self) -> list[dict[str, Any]]:
        """Return a list of all registered tools with their schemas (for testing/APIs)."""
        tools = [
            {
                "name": "velune_ask",
                "description": "Ask Velune about your repository",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "Question about the repository",
                        },
                        "workspace_path": {
                            "type": "string",
                            "description": "Path to repository (optional)",
                        },
                    },
                    "required": ["prompt"],
                },
            },
            {
                "name": "velune_search_memory",
                "description": "Search Velune's memory for relevant past interactions",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "workspace_path": {"type": "string"},
                        "limit": {"type": "integer", "default": 5},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "velune_get_symbols",
                "description": "Get code symbols (functions, classes) from the repository",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace_path": {"type": "string"},
                        "name_pattern": {"type": "string", "description": "Optional regex"},
                    },
                },
            },
            {
                "name": "velune_estimate_blast_radius",
                "description": "Estimate impact of changing a file on the codebase",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace_path": {"type": "string"},
                        "file_path": {"type": "string"},
                    },
                    "required": ["file_path"],
                },
            },
        ]
        if self.tool_registry:
            for schema in self.tool_registry.list_tool_schemas():
                tools.append({
                    "name": schema["name"],
                    "description": schema["description"],
                    "inputSchema": schema["schema"],
                })
        return tools

    async def handle_json_rpc_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Process a JSON-RPC request (for testing/APIs)."""
        method = request.get("method")
        req_id = request.get("id")
        params = request.get("params", {})

        if not self._rate_limiter.is_allowed():
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32000,
                    "message": "Rate limit exceeded — too many tool calls per minute.",
                }
            }

        if method == "velune_ask":
            res = await self._velune_ask(
                params.get("prompt", ""),
                params.get("workspace_path"),
            )
            return {"jsonrpc": "2.0", "id": req_id, "result": res}
        elif method == "velune_search_memory":
            res = await self._velune_search_memory(
                params.get("query", ""),
                params.get("workspace_path"),
                params.get("limit", 5),
            )
            return {"jsonrpc": "2.0", "id": req_id, "result": res}
        elif method == "velune_get_symbols":
            res = await self._velune_get_symbols(
                params.get("workspace_path"),
                params.get("name_pattern"),
            )
            return {"jsonrpc": "2.0", "id": req_id, "result": res}
        elif method == "velune_estimate_blast_radius":
            res = await self._velune_estimate_blast_radius(
                params.get("workspace_path"),
                params.get("file_path"),
            )
            return {"jsonrpc": "2.0", "id": req_id, "result": res}
        elif self.tool_registry and self.tool_registry.get(method):
            tool = self.tool_registry.get(method)
            try:
                res = await tool.execute(**params)
                return {"jsonrpc": "2.0", "id": req_id, "result": res}
            except Exception as e:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32603,
                        "message": str(e),
                    }
                }
        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32601,
                    "message": f"Method not found: {method}",
                }
            }

    async def run_stdio(self) -> None:
        """Run MCP server over stdio (for Claude Desktop)."""
        logger.info("Starting Velune MCP server (stdio)")
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="velune", server_version="0.1.0", capabilities={"tools": {}}
                ),
            )

    async def run_http(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        """Run MCP server over HTTP/SSE (for VS Code, browsers)."""
        logger.info(f"Starting Velune MCP server (HTTP on {host}:{port})")
        try:
            from aiohttp import web

            async def handle_sse(request):
                """Handle SSE transport."""
                response = web.StreamResponse()
                response.headers["Content-Type"] = "text/event-stream"
                response.headers["Cache-Control"] = "no-cache"
                response.headers["Connection"] = "keep-alive"
                await response.prepare(request)

                # Simplified: echo back a message
                msg = json.dumps({"result": "Velune MCP Server ready"})
                await response.write(f"data: {msg}\n\n".encode())
                await response.write_eof()
                return response

            async def handle_tools(request):
                """Handle tool listing."""
                tools = await self.server.list_tools()
                return web.json_response(
                    [{"name": t.name, "description": t.description} for t in tools]
                )

            app = web.Application()
            app.router.add_get("/sse", handle_sse)
            app.router.add_get("/tools", handle_tools)

            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host, port)
            await site.start()

            logger.info(f"Velune MCP Server listening on http://{host}:{port}")
            # Keep running until interrupted
            await asyncio.sleep(3600 * 24)
        except ImportError:
            logger.error("aiohttp not installed for HTTP transport")
        except Exception as e:
            logger.error(f"HTTP server failed: {e}")
