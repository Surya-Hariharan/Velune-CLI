"""Docker container endpoint discovery."""

import asyncio
import logging
import httpx

from velune.core.types.provider import ProviderHealth

logger = logging.getLogger("velune.providers.discovery.docker")

COMMON_PORTS = [8000, 8080, 11434, 1234]

async def _check_endpoint(port: int) -> dict | None:
    """Check if a specific port hosts an OpenAI-compatible /v1/models endpoint."""
    url = f"http://localhost:{port}/v1/models"
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                if "data" in data and isinstance(data["data"], list):
                    return {
                        "port": port,
                        "url": f"http://localhost:{port}/v1",
                        "status": ProviderHealth.HEALTHY,
                        "model_count": len(data["data"]),
                    }
    except Exception:
        pass
    return None

async def discover_docker_endpoints() -> list[dict]:
    """Scan common local inference ports to discover active OpenAI-compatible backends."""
    tasks = [_check_endpoint(port) for port in COMMON_PORTS]
    results = await asyncio.gather(*tasks)
    return [r for r in results if r is not None]
