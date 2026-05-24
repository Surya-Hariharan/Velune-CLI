import pytest
import tempfile
import pathlib
import os
import asyncio
import sys
from unittest.mock import MagicMock, AsyncMock, patch

from velune.daemon.transport import IpcServer, IpcClient, get_ipc_address, DAEMON_PID_FILE
from velune.daemon.client import DaemonClient
from velune.daemon.server import VeluneDaemon

@pytest.mark.asyncio
async def test_ipc_server_client_ping_communication():
    # We use a unique temporary address/named pipe for test isolation
    if sys.platform == "win32":
        import uuid
        test_address = f"\\\\.\\pipe\\velune_daemon_test_{uuid.uuid4().hex}"
    else:
        # Use a temporary file for socket
        tmp_dir = tempfile.mkdtemp()
        test_address = str(pathlib.Path(tmp_dir) / "daemon_test.sock")
        
    received_requests = []
    
    async def mock_callback(request):
        received_requests.append(request)
        if request.get("command") == "ping":
            return {"status": "ok", "pid": 12345}
        return {"status": "unknown"}

    server = IpcServer(test_address, mock_callback)
    await server.start()
    serve_task = asyncio.create_task(server.serve_forever())
    
    try:
        # Verify client thinks server is running
        assert IpcClient.is_running(test_address) is True
        
        # Yield to event loop to allow accept loop to bind next named pipe instance
        await asyncio.sleep(0.1)
        
        # Send command
        response = await IpcClient.send_command(test_address, "ping", extra="args")

        assert response["status"] == "ok"
        assert response["pid"] == 12345
        assert len(received_requests) == 1
        assert received_requests[0]["command"] == "ping"
        assert received_requests[0]["extra"] == "args"
        
    finally:
        server.close()
        serve_task.cancel()
        try:
            await serve_task
        except asyncio.CancelledError:
            pass
        
        # Clean up temporary socket
        if sys.platform != "win32":
            try:
                pathlib.Path(test_address).unlink()
                os.rmdir(tmp_dir)
            except Exception:
                pass

@pytest.mark.asyncio
async def test_velune_daemon_startup_and_dispatch():
    # Setup mock runtime
    mock_runtime = MagicMock()
    mock_lifecycle = AsyncMock()
    mock_registry = MagicMock()
    
    mock_model = MagicMock()
    mock_model.to_dict.return_value = {"id": "gpt-mock", "name": "GPT Mock"}
    mock_registry.list_models.return_value = [mock_model]
    
    mock_runtime.container.get.side_effect = lambda key: {
        "runtime.lifecycle": mock_lifecycle,
        "models.registry": mock_registry
    }[key]
    
    with patch("velune.core.runtime.build_runtime", return_value=mock_runtime):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            test_workspace = pathlib.Path(tmpdir)
            
            daemon = VeluneDaemon(test_workspace)
            
            # We patch the server startup to use a test socket/pipe path
            if sys.platform == "win32":
                import uuid
                test_address = f"\\\\.\\pipe\\velune_daemon_test_srv_{uuid.uuid4().hex}"
            else:
                test_address = str(test_workspace / "daemon.sock")
                
            with patch("velune.daemon.server.get_ipc_address", return_value=test_address):
                # Start daemon in a background task
                daemon_task = asyncio.create_task(daemon.start())
                
                # Wait briefly for startup
                await asyncio.sleep(0.5)
                
                try:
                    # Test ping
                    ping_res = await IpcClient.send_command(test_address, "ping")
                    assert ping_res["status"] == "ok"
                    assert ping_res["pid"] == os.getpid()
                    
                    # Yield to allow accept loop to bind next named pipe instance
                    await asyncio.sleep(0.1)
                    
                    # Test models list
                    models_res = await IpcClient.send_command(test_address, "models_list")
                    assert models_res["status"] == "success"
                    assert len(models_res["models"]) == 1
                    assert models_res["models"][0]["id"] == "gpt-mock"
                    
                    # Yield to allow accept loop to bind next named pipe instance
                    await asyncio.sleep(0.1)
                    
                    # Test ask
                    ask_res = await IpcClient.send_command(test_address, "ask", prompt="hello")
                    assert ask_res["status"] == "success"
                    assert "Phase 2" in ask_res["response"]
                    
                finally:
                    daemon.shutdown()
                    # Cancel daemon task to stop serve_forever
                    daemon_task.cancel()
                    try:
                        await daemon_task
                    except asyncio.CancelledError:
                        pass
