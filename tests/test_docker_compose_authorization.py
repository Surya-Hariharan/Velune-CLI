"""Docker `compose up` and option-like names, through the real gate (VAIR Decision E).

The compose file decides how risky `compose up` is: a file that asks for a
privileged container, the host network, a host bind mount, the Docker socket or a
device must never start unattended in AUTO mode.
"""

from __future__ import annotations

import pytest

from tests.docker_fakes import FakeDaemon, make_engine
from velune.permissions.boundary import Boundary
from velune.permissions.gate import Approval, PermissionGate
from velune.permissions.policy import ExecutionMode, PolicyState
from velune.resources.base import ErrorCode, ResourceResult
from velune.resources.docker.connector import DockerConnector
from velune.resources.manager import ResourceManager
from velune.resources.ownership import ResourceRef
from velune.tools.base.tool import ToolCallContext


def _manager(workspace, mode, *, ask=None):
    records: list[dict] = []
    ran: list[list[str]] = []

    def factory():
        gate = PermissionGate(
            state=PolicyState(mode=mode),
            boundary=Boundary(workspace),
            ask=ask,
            audit=records.append,
        )
        return ToolCallContext(run_id="t", actor="test", workspace=workspace, gate=gate)

    class FakeDocker(DockerConnector):
        @staticmethod
        def _docker_path():
            return "docker"

        async def _run(self, args, *, timeout=20.0):
            ran.append(args)
            return ResourceResult.success(action=args[0], data="")

    daemon = FakeDaemon()
    daemon.add_container("web-1", logs=b"hello")
    manager = ResourceManager(workspace=workspace, context_factory=factory)
    manager.register(
        "docker",
        lambda: FakeDocker(engine=make_engine(daemon), workspace=workspace, workspace_trusted=True),
    )
    manager.get("docker")._has_compose = True
    manager.daemon = daemon
    return manager, records, ran


def _with_compose(tmp_path, monkeypatch, mode, text, *, ask=None):
    (tmp_path / "compose.yaml").write_text(text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)  # `docker compose` runs in the process cwd
    return _manager(tmp_path, mode, ask=ask)


ORDINARY = """\
services:
  web:
    image: nginx
    ports:
      - "127.0.0.1:8080:80"
    volumes:
      - ./site:/usr/share/nginx/html
"""


async def test_auto_runs_compose_up_for_an_ordinary_project(tmp_path, monkeypatch):
    m, _, ran = _with_compose(tmp_path, monkeypatch, ExecutionMode.AUTO, ORDINARY)
    assert (await m.execute("docker", "compose_up")).ok
    assert "up" in ran[-1] and ran[-1][0] == "compose" and "-p" in ran[-1]


@pytest.mark.parametrize(
    ("snippet", "scope"),
    [
        ("    privileged: true", "docker.container.privileged"),
        ("    network_mode: host", "docker.network.host"),
        ("    volumes:\n      - /:/host", "docker.mount.host"),
        (
            "    volumes:\n      - /var/run/docker.sock:/var/run/docker.sock",
            "docker.mount.host",
        ),
        ("    devices:\n      - /dev/sda:/dev/sda", "docker.device.access"),
        ("    cap_add:\n      - SYS_ADMIN", "docker.container.privileged"),
    ],
)
async def test_auto_never_starts_risky_compose_projects_unattended(
    tmp_path, monkeypatch, snippet, scope
):
    text = f"services:\n  app:\n    image: x\n{snippet}\n"
    m, records, ran = _with_compose(tmp_path, monkeypatch, ExecutionMode.AUTO, text)
    result = await m.execute("docker", "compose_up")
    assert result.denied and ran == []
    scopes = {a["scope"] for a in records[-1]["actions"]}
    assert scope in scopes and "docker.compose.up" in scopes
    assert all(a["risk"] in ("medium", "high") for a in records[-1]["actions"])


async def test_a_human_can_approve_a_risky_compose_project(tmp_path, monkeypatch):
    seen: list[str] = []

    async def ask(tool, decision):
        seen.append(decision.reason)
        return Approval.ALLOW_ONCE

    text = "services:\n  a:\n    image: x\n    privileged: true\n"
    m, _, ran = _with_compose(tmp_path, monkeypatch, ExecutionMode.MANUAL, text, ask=ask)
    assert (await m.execute("docker", "compose_up")).ok
    assert ran and "privileged" in seen[0]


async def test_a_privileged_project_is_never_covered_by_a_session_grant(tmp_path, monkeypatch):
    asked = 0

    async def ask(tool, decision):
        nonlocal asked
        asked += 1
        return Approval.ALLOW_TASK

    text = "services:\n  a:\n    image: x\n    privileged: true\n"
    m, _, _ = _with_compose(tmp_path, monkeypatch, ExecutionMode.MANUAL, text, ask=ask)
    await m.execute("docker", "compose_up")
    await m.execute("docker", "compose_up")
    assert asked == 2


async def test_compose_include_is_treated_as_unverified(tmp_path, monkeypatch):
    m, records, ran = _with_compose(
        tmp_path, monkeypatch, ExecutionMode.AUTO, "include:\n  - other.yml\n"
    )
    assert (await m.execute("docker", "compose_up")).denied and ran == []
    assert "docker.compose.unverified" in {a["scope"] for a in records[-1]["actions"]}


async def test_plan_mode_refuses_compose_up_even_for_an_ordinary_project(tmp_path, monkeypatch):
    m, _, ran = _with_compose(tmp_path, monkeypatch, ExecutionMode.PLAN, ORDINARY)
    assert (await m.execute("docker", "compose_up")).denied and ran == []


async def test_compose_file_outside_the_workspace_is_refused_in_auto(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "evil.yaml").write_text("services: {}\n")
    m, _, ran = _manager(ws, ExecutionMode.AUTO)
    result = await m.execute("docker", "compose_up", {"file": str(tmp_path / "evil.yaml")})
    assert result.denied and ran == []


async def test_destructive_compose_down_still_asks_in_auto(tmp_path, monkeypatch):
    m, _, ran = _with_compose(tmp_path, monkeypatch, ExecutionMode.AUTO, ORDINARY)
    assert (await m.execute("docker", "compose_down")).denied and ran == []


# ── Names are never parsed as docker options ────────────────────────────────


@pytest.mark.parametrize(
    ("action", "params"),
    [
        ("inspect", {"container": "--help"}),
        ("logs", {"container": "-f"}),
        ("logs", {"container": "web", "tail": "--follow"}),
        ("inspect", {"image": "-a"}),
        ("build", {"path": "--help", "tag": "x"}),
        ("compose_up", {"file": "-v"}),
        ("stop", {"container": " --all"}),
    ],
)
async def test_option_like_values_never_reach_docker(tmp_path, action, params):
    m, _, ran = _manager(tmp_path, ExecutionMode.AUTO)
    m.ledger.record_created(ResourceRef("docker", "container", params.get("container", "x")))
    result = await m.execute("docker", action, params)
    assert not result.ok and ran == []
    # Reads reach the connector and fail validation there; changes may instead be
    # refused earlier by the gate. Docker is never invoked either way.
    assert result.code in (ErrorCode.VALIDATION_FAILED, ErrorCode.AUTHORIZATION_DENIED)


async def test_read_actions_with_option_like_names_fail_validation(tmp_path):
    m, _, ran = _manager(tmp_path, ExecutionMode.PLAN)
    result = await m.execute("docker", "inspect", {"container": "--help"})
    assert result.code is ErrorCode.VALIDATION_FAILED and ran == []


async def test_ordinary_names_still_work(tmp_path):
    m, _, ran = _manager(tmp_path, ExecutionMode.PLAN)
    assert (await m.execute("docker", "inspect", {"container": "web-1"})).ok
    logs = await m.execute("docker", "logs", {"container": "web-1", "tail": 50})
    assert logs.ok and "hello" in logs.data["text"]
    assert [a for a in ran if a[0] != "compose"] == []  # native SDK reads never shell out
