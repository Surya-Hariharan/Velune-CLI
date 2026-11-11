"""Optional checks against a real, disposable local Docker daemon (VAIR A4).

Skipped unless ``VELUNE_DOCKER_LIVE=1`` and a daemon is reachable; the main suite
never needs Docker. Everything it creates is labelled by Velune and removed again.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.live

if os.environ.get("VELUNE_DOCKER_LIVE") != "1":
    pytest.skip(
        "set VELUNE_DOCKER_LIVE=1 to run against a real Docker daemon", allow_module_level=True
    )

from velune.resources.docker.connector import DockerConnector  # noqa: E402


async def test_real_daemon_lifecycle(tmp_path):
    conn = DockerConnector(workspace=tmp_path, workspace_trusted=True, session_id="live")
    connected = await conn.connect()
    if not connected.ok:
        pytest.skip(f"no reachable Docker daemon: {connected.error}")
    try:
        assert (await conn.execute("image_pull", {"image": "hello-world:latest"})).ok
        made = await conn.execute(
            "container_create", {"image": "hello-world:latest", "name": "velune-live-test"}
        )
        assert made.ok and made.data["ownership"] == "velune"
        assert (await conn.execute("start", {"container": "velune-live-test"})).ok
        rows = (await conn.execute("ps", {"all": True})).data
        assert any(r["name"] == "velune-live-test" and r["ownership"] == "velune" for r in rows)
    finally:
        await conn.execute("container_remove", {"container": "velune-live-test", "force": True})
        await conn.disconnect()
