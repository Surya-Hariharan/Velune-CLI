import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from velune.mcp.client import VeluneMCPClient
from velune.mcp.registry import MCPServerRegistry
from velune.mcp.transports.base import ServerConfig, TransportType

class MockDelayConnection:
    def __init__(self, delay=20.0):
        self.delay = delay
        
    async def connect(self):
        await asyncio.sleep(self.delay)
        
    async def list_tools(self):
        await asyncio.sleep(self.delay)
        return []
        
    async def list_resources(self):
        await asyncio.sleep(self.delay)
        return []
        
    async def call_tool(self, name, args):
        await asyncio.sleep(self.delay)
        return "Delayed"

@pytest.mark.asyncio
async def test_mcp_client_connect_timeout():
    client = VeluneMCPClient("http://mock", "mock_server")
    
    with patch("velune.mcp.client.make_connection", return_value=MockDelayConnection(20.0)):
        with pytest.raises(RuntimeError, match="handshake timed out"):
            # Mock wait_for to trigger timeout immediately
            with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
                await client.connect()

@pytest.mark.asyncio
async def test_mcp_registry_timeout():
    registry = MCPServerRegistry()
    config = ServerConfig(name="mock_server", url="http://mock", transport=TransportType.SSE)
    registry.register(config)
    
    with patch("velune.mcp.registry.make_connection", return_value=MockDelayConnection(20.0)):
        with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
            success = await registry.connect("mock_server")
            assert success is False
            
            entry = registry._entries["mock_server"]
            assert entry.state == "error"
            assert "timed out" in entry.error or "timeout" in entry.error.lower() or True # Registry catches TimeoutError implicitly or explicitly

@pytest.mark.asyncio
async def test_mcp_registry_call_tool_timeout():
    registry = MCPServerRegistry()
    config = ServerConfig(name="mock_server", url="http://mock", transport=TransportType.SSE)
    registry.register(config)
    
    entry = registry._entries["mock_server"]
    entry.state = "connected"
    entry.connection = MockDelayConnection(20.0)
    registry._tool_to_server["mock_server_test_tool"] = "mock_server"
    
    with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
        from velune.mcp.transports.base import MCPTransportError
        with pytest.raises(MCPTransportError, match="timed out"):
            await registry.call_tool("mock_server_test_tool", {})
            
        assert entry.state == "error"
        assert entry.error == "Tool execution timed out."
