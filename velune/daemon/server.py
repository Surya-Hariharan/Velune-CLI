import asyncio
import json
import os
from pathlib import Path
from typing import Any

from velune.daemon.transport import IpcServer, get_ipc_address, DAEMON_PID_FILE

class VeluneDaemon:
    """Background daemon holding initialized subsystems."""
    
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.runtime = None  # RuntimeContext
        self._server = None
    
    async def start(self):
        """Initialize runtime and start IPC server."""
        from velune.core.runtime import build_runtime
        self.runtime = build_runtime(self.workspace)
        await self.runtime.container.get("runtime.lifecycle").startup()
        
        self._server = IpcServer(get_ipc_address(), self._dispatch)
        await self._server.start()
        
        # Write PID file
        DAEMON_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        DAEMON_PID_FILE.write_text(str(os.getpid()))
        
        await self._server.serve_forever()
        
    def shutdown(self):
        if self._server:
            self._server.close()
        if DAEMON_PID_FILE.exists():
            try:
                DAEMON_PID_FILE.unlink()
            except Exception:
                pass
    
    async def _dispatch(self, request: dict) -> dict:
        """Route IPC request to appropriate handler."""
        command = request.get("command")
        if command == "ask":
            return await self._handle_ask(request)
        elif command == "models_list":
            return await self._handle_models_list(request)
        elif command == "ping":
            return {"status": "ok", "pid": os.getpid()}
        return {"error": f"Unknown command: {command}"}

    async def _handle_ask(self, request: dict) -> dict:
        # Phase 2 implementation placeholder
        return {"status": "success", "response": "Phase 2 daemon execution."}

    async def _handle_models_list(self, request: dict) -> dict:
        try:
            models = self.runtime.container.get("models.registry").list_models()
            return {"status": "success", "models": [m.to_dict() if hasattr(m, "to_dict") else str(m) for m in models]}
        except Exception as e:
            return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    import sys
    from velune.daemon.client import DaemonClient
    
    if len(sys.argv) < 2:
        print("Usage: python -m velune.daemon.server <workspace_path>")
        sys.exit(1)
        
    if DaemonClient.is_running():
        print("Daemon is already running.")
        sys.exit(0)
        
    workspace_path = Path(sys.argv[1]).resolve()
    daemon = VeluneDaemon(workspace_path)
    try:
        asyncio.run(daemon.start())
    except KeyboardInterrupt:
        daemon.shutdown()
