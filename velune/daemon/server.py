import asyncio
import os
from pathlib import Path

from velune.daemon.transport import DAEMON_PID_FILE, IpcServer, get_ipc_address


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
        elif command == "probe_model":
            return await self._handle_probe_model(request)
        elif command == "ping":
            return {"status": "ok", "pid": os.getpid()}
        return {"error": f"Unknown command: {command}"}

    async def _handle_ask(self, request: dict) -> dict:
        # Phase 2 implementation placeholder
        return {"status": "success", "response": "Phase 2 daemon execution."}

    async def _handle_models_list(self, request: dict) -> dict:
        try:
            models = self.runtime.container.get("runtime.model_registry").list_all()
            return {"status": "success", "models": [m.to_dict() if hasattr(m, "to_dict") else str(m) for m in models]}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def _handle_probe_model(self, request: dict) -> dict:
        model_id = request.get("model_id")
        provider_id = request.get("provider_id")
        if not model_id or not provider_id:
            return {"status": "error", "message": "Missing model_id or provider_id"}

        async def run_probe():
            try:
                provider_reg = self.runtime.container.get("runtime.provider_registry")
                provider = provider_reg.get(provider_id)
                if not provider:
                    return

                from velune.models.probes import ModelProber
                from velune.models.profile_cache import ModelProfileCache
                profile_cache = ModelProfileCache(self.workspace / ".velune" / "model_profiles.json")

                prober = ModelProber(provider, model_id)
                results = await prober.run_all_probes()
                profile_cache.set(model_id, provider_id, results)

                # Also apply in-memory to daemon's registry
                registry = self.runtime.container.get("runtime.model_registry")
                if registry:
                    model = registry.get(model_id, provider_id)
                    if model:
                        registry._apply_probe_results(model, results)
            except Exception:
                pass

        asyncio.create_task(run_probe())
        return {"status": "success", "message": f"Started probing for model {model_id} in daemon background."}

if __name__ == "__main__":
    import sys

    from velune.daemon.client import DaemonClient
    from velune.kernel.entrypoint import run_async

    if len(sys.argv) < 2:
        print("Usage: python -m velune.daemon.server <workspace_path>")
        sys.exit(1)

    if DaemonClient.is_running():
        print("Daemon is already running.")
        sys.exit(0)

    workspace_path = Path(sys.argv[1]).resolve()
    daemon = VeluneDaemon(workspace_path)
    try:
        run_async(daemon.start())
    except KeyboardInterrupt:
        daemon.shutdown()
