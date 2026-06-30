"""Docker container endpoint discovery.

Scans localhost ports commonly used by Docker containers running OpenAI-compatible
inference servers.  Ports 1234 (LM Studio) and 11434 (Ollama) are excluded because
those have their own dedicated discoverers.

The ``DockerDiscovery`` class returns ``ModelDescriptor`` objects and is wired into
the central ``ModelDiscoveryScanner`` alongside the other backends.

The legacy ``discover_docker_endpoints()`` function is kept for backward compatibility
but is no longer used by the scanner.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor

logger = logging.getLogger("velune.providers.discovery.docker")

# Ports checked for Docker-hosted OpenAI-compatible servers.
# Excludes 1234 (LM Studio) and 11434 (Ollama) to avoid double-listing.
_DOCKER_PORTS: tuple[int, ...] = (8000, 8080, 8888, 9000, 7860, 5000)


async def _probe_port(port: int) -> list[ModelDescriptor]:
    """Return ModelDescriptors found at *port*, or [] if unreachable/no models."""
    base_url = f"http://localhost:{port}/v1"
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{base_url}/models")
            if r.status_code != 200:
                return []
            data = r.json()
            items = data.get("data", [])
    except Exception:
        return []

    models: list[ModelDescriptor] = []
    for item in items:
        model_id = item.get("id")
        if not model_id:
            continue
        caps = _classify_capabilities(model_id)
        tags = ["local", "docker"]
        if caps.vision > CapabilityLevel.NONE:
            tags.append("vision")
        if caps.embedding > CapabilityLevel.NONE:
            tags.append("embedding")
        models.append(
            ModelDescriptor(
                model_id=model_id,
                provider_id="docker",
                display_name=model_id,
                context_length=item.get("context_window", 8192),
                capabilities=caps,
                is_local=True,
                location=base_url,
                health="unknown",
                tags=tags,
                metadata={"port": port, "base_url": base_url, "raw": item},
            )
        )
    return models


def _classify_capabilities(model_id: str) -> ModelCapabilityProfile:
    lower = model_id.lower()
    profile = ModelCapabilityProfile()

    if any(kw in lower for kw in ["coder", "code", "starcoder"]):
        profile.coding = CapabilityLevel.INTERMEDIATE
    else:
        profile.coding = CapabilityLevel.BASIC

    if any(kw in lower for kw in ["r1", "reason", "qwq"]):
        profile.reasoning = CapabilityLevel.ADVANCED
    else:
        profile.reasoning = CapabilityLevel.BASIC

    if any(kw in lower for kw in ["embed", "bge-", "e5-", "gte-"]):
        profile.embedding = CapabilityLevel.ADVANCED

    if any(kw in lower for kw in ["vision", "llava", "vl", "moondream", "minicpm-v"]):
        profile.vision = CapabilityLevel.ADVANCED
        profile.multimodal = CapabilityLevel.ADVANCED

    if any(kw in lower for kw in ["instruct", "chat"]):
        profile.instruction_following = CapabilityLevel.INTERMEDIATE
        profile.tool_use = CapabilityLevel.INTERMEDIATE

    return profile


class DockerDiscovery:
    """Discovers models from Docker containers exposing an OpenAI-compatible API."""

    provider_id = "docker"

    async def discover(self) -> list[ModelDescriptor]:
        """Scan Docker-typical ports in parallel and return all found models."""
        results = await asyncio.gather(
            *[_probe_port(port) for port in _DOCKER_PORTS],
            return_exceptions=True,
        )
        seen: set[str] = set()
        models: list[ModelDescriptor] = []
        for result in results:
            if isinstance(result, list):
                for m in result:
                    if m.model_id not in seen:
                        seen.add(m.model_id)
                        models.append(m)
        return models


# ---------------------------------------------------------------------------
# Legacy function kept for backward compatibility
# ---------------------------------------------------------------------------


async def discover_docker_endpoints() -> list[dict]:
    """[Deprecated] Return dicts for each reachable OpenAI-compatible port."""
    from velune.core.types.provider import ProviderHealth  # type: ignore[attr-defined]

    ports = [8000, 8080, 11434, 1234]
    results: list[dict] = []

    async def _check(port: int) -> dict | None:
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

    checks = await asyncio.gather(*[_check(p) for p in ports])
    results = [r for r in checks if r is not None]
    return results
