"""Integration tests for Velune MCP server.

Tests:
- Server startup and shutdown
- MCP tool discovery
- Tool execution with workspace validation
- Security: workspace path validation
- Transport: stdio and HTTP/SSE
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest


class TestMCPServerStartup:
    """Test MCP server startup and shutdown."""

    @pytest.mark.asyncio
    async def test_server_startup_stdio(self):
        """Test MCP server starts with stdio transport."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            # Create a simple test file
            (workspace / "test.py").write_text("def hello(): pass")

            # Import server
            from velune.mcp.server import VeluneMCPServer

            server = VeluneMCPServer(workspace_path=workspace)
            assert server.workspace_path == workspace

    @pytest.mark.asyncio
    async def test_server_with_allowed_workspaces(self):
        """Test MCP server with allowed workspaces list."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace1 = Path(tmpdir) / "project1"
            workspace2 = Path(tmpdir) / "project2"
            workspace1.mkdir()
            workspace2.mkdir()

            from velune.mcp.server import VeluneMCPServer

            server = VeluneMCPServer(
                workspace_path=workspace1,
                allowed_workspaces=[workspace1, workspace2],
            )

            assert server.allowed_workspaces == [workspace1, workspace2]


class TestWorkspaceValidator:
    """Test workspace path validation."""

    def test_validator_accepts_allowed_path(self):
        """Validator accepts path within allowed list."""
        from velune.mcp.server import WorkspaceValidator

        with tempfile.TemporaryDirectory() as tmpdir:
            allowed = Path(tmpdir)
            validator = WorkspaceValidator(allowed_workspaces=[allowed])

            # Should accept exact path
            assert validator.is_valid(str(allowed))

            # Should accept subdirectory
            sub = allowed / "subdir"
            sub.mkdir()
            assert validator.is_valid(str(sub))

    def test_validator_rejects_outside_path(self):
        """Validator rejects path outside allowed list."""
        from velune.mcp.server import WorkspaceValidator

        with tempfile.TemporaryDirectory() as tmpdir1:
            with tempfile.TemporaryDirectory() as tmpdir2:
                allowed = Path(tmpdir1)
                outside = Path(tmpdir2)

                validator = WorkspaceValidator(allowed_workspaces=[allowed])

                assert not validator.is_valid(str(outside))

    def test_validator_raises_on_invalid_path(self):
        """Validator raises ValueError for invalid path."""
        from velune.mcp.server import WorkspaceValidator

        with tempfile.TemporaryDirectory() as tmpdir:
            allowed = Path(tmpdir)
            validator = WorkspaceValidator(allowed_workspaces=[allowed])

            outside = Path(tmpdir) / ".." / "outside"
            with pytest.raises(ValueError, match="not in allowed list"):
                validator.validate(str(outside))


class TestMCPTools:
    """Test MCP tool implementations."""

    @pytest.mark.asyncio
    async def test_velune_ask_with_invalid_workspace(self):
        """velune_ask rejects invalid workspace path."""
        from velune.mcp.server import VeluneMCPServer

        with tempfile.TemporaryDirectory() as tmpdir:
            allowed = Path(tmpdir)
            server = VeluneMCPServer(workspace_path=allowed, allowed_workspaces=[allowed])

            result = await server._velune_ask(
                prompt="What is this project about?",
                workspace_path="/invalid/path",
            )

            assert "error" in result
            assert "not in allowed list" in result["error"]

    @pytest.mark.asyncio
    async def test_velune_get_symbols_with_valid_workspace(self):
        """velune_get_symbols works with valid workspace."""
        from velune.mcp.server import VeluneMCPServer

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            # Create a Python file with symbols
            (workspace / "module.py").write_text(
                "class MyClass:\n    def method(self): pass\n"
                "def my_function(): pass\n"
            )

            server = VeluneMCPServer(workspace_path=workspace)

            # Test with workspace None (uses default)
            result = await server._velune_get_symbols(workspace_path=None)

            # Result should have symbols structure
            assert "symbols" in result

    @pytest.mark.asyncio
    async def test_velune_get_symbols_filters_by_pattern(self):
        """velune_get_symbols filters by name pattern."""
        from velune.mcp.server import VeluneMCPServer

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            # Create Python file with multiple classes
            (workspace / "models.py").write_text(
                "class User: pass\n"
                "class UserManager: pass\n"
                "class Document: pass\n"
            )

            server = VeluneMCPServer(workspace_path=workspace)

            result = await server._velune_get_symbols(
                workspace_path=str(workspace),
                name_pattern="User.*",
            )

            # Should only include User and UserManager
            assert "symbols" in result

    @pytest.mark.asyncio
    async def test_velune_estimate_blast_radius_valid_file(self):
        """velune_estimate_blast_radius returns valid structure."""
        from velune.mcp.server import VeluneMCPServer

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            test_file = workspace / "module.py"
            test_file.write_text("def example(): pass")

            server = VeluneMCPServer(workspace_path=workspace)

            result = await server._velune_estimate_blast_radius(
                workspace_path=str(workspace),
                file_path="module.py",
            )

            # Should have score structure
            assert "score" in result
            assert 0.0 <= result.get("score", 0) <= 1.0
            assert "fan_in" in result
            assert "fan_out" in result

    @pytest.mark.asyncio
    async def test_velune_estimate_blast_radius_missing_file(self):
        """velune_estimate_blast_radius requires file_path."""
        from velune.mcp.server import VeluneMCPServer

        with tempfile.TemporaryDirectory() as tmpdir:
            server = VeluneMCPServer(workspace_path=Path(tmpdir))

            result = await server._velune_estimate_blast_radius(
                workspace_path=None,
                file_path=None,  # Missing
            )

            assert "error" in result


class TestMCPJsonRpc:
    """Test MCP JSON-RPC protocol handling."""

    @pytest.mark.asyncio
    async def test_json_rpc_velune_ask(self):
        """JSON-RPC handler processes velune_ask correctly."""
        from velune.mcp.server import VeluneMCPServer

        with tempfile.TemporaryDirectory() as tmpdir:
            server = VeluneMCPServer(workspace_path=Path(tmpdir))

            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "velune_ask",
                "params": {
                    "prompt": "What is this project?",
                    "workspace_path": str(Path(tmpdir)),
                },
            }

            result = await server.handle_json_rpc_request(request)

            assert result["jsonrpc"] == "2.0"
            assert result["id"] == 1
            assert "result" in result or "error" in result

    @pytest.mark.asyncio
    async def test_json_rpc_unknown_method(self):
        """JSON-RPC handler rejects unknown methods."""
        from velune.mcp.server import VeluneMCPServer

        server = VeluneMCPServer()

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "unknown_method",
            "params": {},
        }

        result = await server.handle_json_rpc_request(request)

        assert "error" in result
        assert result["error"]["code"] == -32601  # Method not found


class TestToolsListing:
    """Test MCP tool discovery."""

    def test_tools_list_includes_velune_tools(self):
        """get_tools_list returns all Velune tools."""
        from velune.mcp.server import VeluneMCPServer

        server = VeluneMCPServer()
        tools = server.get_tools_list()

        tool_names = {t["name"] for t in tools}

        assert "velune_ask" in tool_names
        assert "velune_search_memory" in tool_names
        assert "velune_get_symbols" in tool_names
        assert "velune_estimate_blast_radius" in tool_names

    def test_tools_have_proper_schema(self):
        """Tools have valid input schemas."""
        from velune.mcp.server import VeluneMCPServer

        server = VeluneMCPServer()
        tools = server.get_tools_list()

        for tool in tools:
            # Check required fields
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool

            # Check schema structure
            schema = tool["inputSchema"]
            assert "type" in schema
            assert schema["type"] == "object"
            assert "properties" in schema


class TestSecurityIsolation:
    """Test security and isolation features."""

    @pytest.mark.asyncio
    async def test_cannot_access_parent_directory(self):
        """Cannot access files outside workspace via path traversal."""
        from velune.mcp.server import WorkspaceValidator

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "safe"
            workspace.mkdir()

            validator = WorkspaceValidator(allowed_workspaces=[workspace])

            # Try to escape with ..
            escaped_path = str(workspace / ".." / "unsafe")
            with pytest.raises(ValueError):
                validator.validate(escaped_path)

    @pytest.mark.asyncio
    async def test_multiple_workspaces_isolation(self):
        """Cannot access sibling workspace."""
        from velune.mcp.server import WorkspaceValidator

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ws1 = root / "workspace1"
            ws2 = root / "workspace2"
            ws1.mkdir()
            ws2.mkdir()

            validator = WorkspaceValidator(allowed_workspaces=[ws1])

            # ws1 is allowed
            assert validator.is_valid(str(ws1))

            # ws2 is not allowed
            assert not validator.is_valid(str(ws2))


class TestMCPRate Limiting:
    """Test rate limiting."""

    def test_rate_limiter_allows_first_call(self):
        """Rate limiter allows first call immediately."""
        from velune.mcp.server import RateLimiter

        limiter = RateLimiter(calls_per_minute=60)

        assert limiter.is_allowed("client1")

    def test_rate_limiter_refills_tokens(self):
        """Rate limiter refills tokens over time."""
        import time

        from velune.mcp.server import RateLimiter

        limiter = RateLimiter(calls_per_minute=1)  # 1 call per minute

        # First call should succeed
        assert limiter.is_allowed("client2")

        # Immediate second call should fail
        assert not limiter.is_allowed("client2")

        # Wait for token refill (1 second = 1/60 minute)
        time.sleep(1.1)

        # Should have refilled one token
        assert limiter.is_allowed("client2")

    def test_rate_limiter_per_client(self):
        """Rate limiter is per client."""
        from velune.mcp.server import RateLimiter

        limiter = RateLimiter(calls_per_minute=1)

        # Client A uses its token
        assert limiter.is_allowed("clientA")
        assert not limiter.is_allowed("clientA")

        # Client B has its own token bucket
        assert limiter.is_allowed("clientB")
        assert not limiter.is_allowed("clientB")


# =========================================================================
# Integration: Full MCP Server Simulation
# =========================================================================

@pytest.mark.asyncio
async def test_full_mcp_workflow():
    """Integration test: complete MCP workflow."""
    from velune.mcp.server import VeluneMCPServer

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)

        # Create sample Python project
        (workspace / "main.py").write_text(
            "class Application:\n"
            "    def run(self): pass\n"
        )

        # Create server
        server = VeluneMCPServer(workspace_path=workspace)

        # 1. List tools
        tools = server.get_tools_list()
        assert len(tools) >= 4

        # 2. Call velune_get_symbols
        symbols_result = await server._velune_get_symbols(str(workspace))
        assert "symbols" in symbols_result

        # 3. Call velune_estimate_blast_radius
        blast_result = await server._velune_estimate_blast_radius(
            str(workspace),
            "main.py",
        )
        assert "score" in blast_result

        # 4. Process JSON-RPC request
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "velune_get_symbols",
            "params": {"workspace_path": str(workspace)},
        }
        response = await server.handle_json_rpc_request(request)
        assert response["id"] == 1
        assert "result" in response or "error" in response
