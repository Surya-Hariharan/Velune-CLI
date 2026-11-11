"""Docker permissions: ownership-aware AUTO/MANUAL/PLAN, escalation, compose, discovery (VAIR A4)."""

from __future__ import annotations

import json

import pytest

from tests.docker_fakes import FakeDaemon, make_engine
from tests.http_env import decide, verdict
from tests.integration_fakes import ScriptedProvider, call, final, make_env, turn
from velune.core.types.inference import ToolCall
from velune.permissions.gate import Approval
from velune.permissions.policy import ExecutionMode
from velune.resources.docker.connector import DockerConnector

MANUAL, AUTO, PLAN = ExecutionMode.MANUAL, ExecutionMode.AUTO, ExecutionMode.PLAN


@pytest.fixture
def daemon():
    d = FakeDaemon()
    d.add_image("nginx:1.27")
    d.add_container("users-db")  # the user's own container
    d.add_volume("user-data")
    d.add_network("corp-net")
    return d


def connector(tmp_path, daemon, **kw):
    return DockerConnector(
        engine=make_engine(daemon),
        workspace=tmp_path,
        workspace_trusted=True,
        session_id="s1",
        **kw,
    )


async def owned_web(tmp_path, daemon, mode=AUTO):
    conn = connector(tmp_path, daemon)
    env = make_env(tmp_path, mode, connectors={"docker": lambda: conn})
    made = await env.manager.execute(
        "docker", "container_create", {"image": "nginx:1.27", "name": "web"}
    )
    assert made.ok, made.error
    return conn, env


def v(conn, action, params, mode, tmp_path):
    return verdict(conn, action, params, mode, tmp_path)


# ── The two headline guarantees ─────────────────────────────────────────────


async def test_a_model_cannot_delete_a_users_container_in_auto_mode(tmp_path, daemon):
    conn = connector(tmp_path, daemon)
    env = make_env(tmp_path, AUTO, connectors={"docker": lambda: conn})
    for params in ({"container": "users-db"}, {"container": "users-db", "force": True}):
        result = await env.manager.execute("docker", "container_remove", params)
        assert (
            result.denied and "users-db" in daemon.containers.values().__iter__().__next__()["Name"]
        )
    assert len(daemon.containers) == 1 and daemon.calls == []


async def test_a_task_owned_container_can_be_stopped_started_and_restarted_in_auto_mode(
    tmp_path, daemon
):
    conn, env = await owned_web(tmp_path, daemon)
    for action in ("start", "restart", "stop"):
        assert (await env.manager.execute("docker", action, {"container": "web"})).ok
    assert [c[0] for c in daemon.calls] == ["running", "running", "exited"]


async def test_an_owned_container_can_be_removed_in_auto_mode_but_not_forced(tmp_path, daemon):
    conn, env = await owned_web(tmp_path, daemon)
    forced = await env.manager.execute(
        "docker", "container_remove", {"container": "web", "force": True}
    )
    assert forced.denied
    assert (await env.manager.execute("docker", "container_remove", {"container": "web"})).ok
    assert [k for k in daemon.containers if daemon.containers[k]["Name"] == "/web"] == []


# ── Ownership-driven decisions ──────────────────────────────────────────────


@pytest.mark.parametrize("action", ["start", "stop", "restart", "pause", "unpause"])
def test_lifecycle_of_an_existing_container_is_high_risk_in_every_mode(tmp_path, daemon, action):
    conn = connector(tmp_path, daemon)
    conn.engine.connect_sync()
    conn._bind_store(conn.engine.client)
    conn._bound = True
    for mode in (MANUAL, AUTO):
        assert v(conn, action, {"container": "users-db"}, mode, tmp_path) == "ask"
    assert v(conn, action, {"container": "users-db"}, PLAN, tmp_path) == "deny"


@pytest.mark.parametrize("mode", [AUTO, MANUAL])
def test_an_unknown_or_unreachable_target_is_never_assumed_owned(tmp_path, daemon, mode):
    conn = connector(tmp_path, daemon)  # not connected: nothing can be looked up
    assert v(conn, "stop", {"container": "web"}, mode, tmp_path) == "ask"


async def test_owned_lifecycle_matrix(tmp_path, daemon):
    conn, _ = await owned_web(tmp_path, daemon)
    assert v(conn, "stop", {"container": "web"}, AUTO, tmp_path) == "allow"
    assert v(conn, "stop", {"container": "web"}, MANUAL, tmp_path) == "ask"
    assert v(conn, "stop", {"container": "web"}, PLAN, tmp_path) == "deny"
    assert v(conn, "container_remove", {"container": "web"}, AUTO, tmp_path) == "allow"


async def test_reads_run_in_every_mode(tmp_path, daemon):
    conn = connector(tmp_path, daemon)
    for action, params in (
        ("ps", {}),
        ("images", {}),
        ("inspect", {"target": "users-db"}),
        ("logs", {"container": "users-db"}),
        ("health", {"container": "x"}),
        ("stats", {"container": "x"}),
        ("info", {}),
        ("volume_ls", {}),
        ("network_ls", {}),
        ("wait_ready", {"container": "x"}),
        ("compose_config", {}),
    ):
        for mode in (MANUAL, AUTO, PLAN):
            assert v(conn, action, params, mode, tmp_path) == "allow", (action, mode)


@pytest.mark.parametrize(
    ("action", "params"),
    [
        ("container_create", {"image": "nginx:1.27"}),
        ("image_tag", {"image": "nginx:1.27", "repository": "a", "tag": "b"}),
        ("volume_create", {"name": "v"}),
        ("network_create", {"name": "n"}),
        ("image_pull", {"image": "redis:7"}),
        ("container_remove", {"container": "x"}),
        ("volume_remove", {"name": "v"}),
        ("image_remove", {"image": "nginx:1.27"}),
        ("network_remove", {"network": "n"}),
        ("compose_up", {}),
        ("compose_down", {}),
        ("compose_stop", {}),
        ("build", {"path": "."}),
    ],
)
def test_plan_mode_never_executes_a_mutation(tmp_path, daemon, action, params):
    assert v(connector(tmp_path, daemon), action, params, PLAN, tmp_path) == "deny"


# ── Removing images, volumes and networks ───────────────────────────────────


@pytest.mark.parametrize(
    ("action", "params"),
    [("image_remove", {"image": "nginx:1.27"}), ("volume_remove", {"name": "user-data"})],
)
@pytest.mark.parametrize("mode", [AUTO, MANUAL])
def test_removing_images_and_volumes_always_needs_a_person(tmp_path, daemon, action, params, mode):
    assert v(connector(tmp_path, daemon), action, params, mode, tmp_path) == "ask"


async def test_even_an_owned_volume_or_image_removal_asks_in_auto(tmp_path, daemon):
    conn, env = await owned_web(tmp_path, daemon)
    await env.manager.execute("docker", "volume_create", {"name": "scratch"})
    assert v(conn, "volume_remove", {"name": "scratch"}, AUTO, tmp_path) == "ask"


async def test_a_network_is_a_change_only_when_owned_and_unused(tmp_path, daemon):
    conn, env = await owned_web(tmp_path, daemon)
    await env.manager.execute("docker", "network_create", {"name": "mine"})
    assert v(conn, "network_remove", {"network": "mine"}, AUTO, tmp_path) == "allow"
    await env.manager.execute("docker", "network_connect", {"network": "mine", "container": "web"})
    assert v(conn, "network_remove", {"network": "mine"}, AUTO, tmp_path) == "ask"  # in use
    assert (
        v(conn, "network_remove", {"network": "corp-net"}, AUTO, tmp_path) == "ask"
    )  # not Velune's
    for builtin in ("bridge", "host", "none"):
        assert (
            v(conn, "network_remove", {"network": builtin}, MANUAL, tmp_path) == "deny"
        )  # blocked outright


async def test_changing_membership_of_foreign_resources_needs_a_person(tmp_path, daemon):
    conn, env = await owned_web(tmp_path, daemon)
    assert (
        v(conn, "network_connect", {"network": "corp-net", "container": "web"}, AUTO, tmp_path)
        == "ask"
    )
    assert (
        v(conn, "network_connect", {"network": "corp-net", "container": "users-db"}, AUTO, tmp_path)
        == "ask"
    )


# ── Creating containers: what makes one extraordinary ───────────────────────


def create(tmp_path, daemon, mode, **extra):
    conn = connector(tmp_path, daemon)
    return decide(conn, "container_create", {"image": "nginx:1.27", **extra}, mode, tmp_path)


def test_an_ordinary_nonprivileged_container_is_allowed_in_auto(tmp_path, daemon):
    (tmp_path / "site").mkdir()
    for extra in (
        {},
        {"ports": ["8080:80"]},
        {"volumes": ["data:/var/lib/data"]},
        {"volumes": ["./site:/usr/share/nginx/html:ro"]},
        {"env": ["MODE=dev"], "memory": "256m", "cpus": 1, "restart_policy": "unless-stopped"},
    ):
        decision, _ = create(tmp_path, daemon, AUTO, **extra)
        assert decision.verdict.value == "allow", extra
    assert create(tmp_path, daemon, MANUAL)[0].verdict.value == "ask"


@pytest.mark.parametrize(
    ("extra", "scope"),
    [
        ({"privileged": True}, "docker.container.privileged"),
        ({"cap_add": ["SYS_ADMIN"]}, "docker.container.privileged"),
        ({"host_network": True}, "docker.network.host"),
        ({"devices": ["/dev/ttyUSB0"]}, "docker.device.access"),
        ({"ports": ["0.0.0.0:80:80"]}, "docker.network.exposed"),
        ({"volumes": ["/var/run/docker.sock:/var/run/docker.sock"]}, "docker.mount.host"),
    ],
)
def test_dangerous_options_always_need_a_person_and_say_why(tmp_path, daemon, extra, scope):
    decision, actions = create(tmp_path, daemon, AUTO, **extra)
    assert decision.verdict.value == "ask"
    assert scope in {a.scope for a in actions} and any(a.risk.value == "high" for a in actions)
    assert create(tmp_path, daemon, PLAN, **extra)[0].verdict.value == "deny"


def test_a_bind_mount_outside_the_workspace_is_refused_in_auto_and_asked_in_manual(
    tmp_path, daemon
):
    (tmp_path / "ws").mkdir()
    conn = connector(tmp_path / "ws", daemon)
    params = {"image": "nginx:1.27", "volumes": [f"{tmp_path}:/host"]}
    assert (
        decide(conn, "container_create", params, AUTO, tmp_path / "ws")[0].verdict.value == "deny"
    )
    assert (
        decide(conn, "container_create", params, MANUAL, tmp_path / "ws")[0].verdict.value == "ask"
    )


def test_mounting_a_secret_inside_the_workspace_needs_a_person(tmp_path, daemon):
    (tmp_path / ".env").write_text("K=v")
    decision, actions = create(tmp_path, daemon, AUTO, volumes=[f"{tmp_path / '.env'}:/app/.env"])
    assert decision.verdict.value == "ask" and any(a.secret for a in actions)


def test_workspace_mounts_can_be_made_to_need_approval(tmp_path, daemon):
    (tmp_path / "site").mkdir()
    conn = connector(tmp_path, daemon, config={"workspace_mounts": False})
    params = {"image": "nginx:1.27", "volumes": ["./site:/x"]}
    assert decide(conn, "container_create", params, AUTO, tmp_path)[0].verdict.value == "ask"


@pytest.mark.parametrize(
    "bad",
    [
        {"image": "--rm"},
        {"image": "nginx:1.27", "volumes": ["x"]},
        {"image": "nginx:1.27", "ports": ["no"]},
    ],
)
def test_invalid_creation_arguments_are_gated_as_a_change_and_never_run(tmp_path, daemon, bad):
    assert (
        decide(connector(tmp_path, daemon), "container_create", bad, PLAN, tmp_path)[
            0
        ].verdict.value
        == "deny"
    )


def test_pulling_is_a_network_operation_to_the_named_registry(tmp_path, daemon):
    conn = connector(tmp_path, daemon)
    decision, actions = decide(conn, "image_pull", {"image": "ghcr.io/org/app:v1"}, AUTO, tmp_path)
    net = [a for a in actions if a.action_type.value == "NETWORK"]
    assert net and net[0].target == "ghcr.io" and decision.verdict.value == "allow"
    assert (
        decide(conn, "image_pull", {"image": "nginx:1"}, MANUAL, tmp_path)[0].verdict.value == "ask"
    )


def test_a_remote_daemon_makes_every_change_high_risk(tmp_path, daemon):
    from velune.resources.docker.engine import Endpoint

    conn = connector(tmp_path, daemon)
    conn.engine.connect_sync()
    conn.engine.endpoint = Endpoint("https://build.example:2376", "config", remote=True)
    assert v(conn, "volume_create", {"name": "v"}, AUTO, tmp_path) == "ask"
    assert v(conn, "ps", {}, AUTO, tmp_path) == "allow"


# ── Compose ─────────────────────────────────────────────────────────────────


def compose(tmp_path, text):
    (tmp_path / "compose.yaml").write_text(text, encoding="utf-8")


def test_compose_external_resources_and_container_namespaces_are_flagged(tmp_path):
    from velune.resources.docker import compose_scan as cs

    assert cs.SCOPE_EXTERNAL in {
        r.scope for r in cs.scan_compose_text("volumes:\n  data:\n    external: true\n")
    }
    assert cs.SCOPE_EXTERNAL in {
        r.scope for r in cs.scan_compose_text("    volumes_from:\n      - other\n")
    }
    assert cs.SCOPE_PRIVILEGED in {
        r.scope for r in cs.scan_compose_text("    network_mode: container:abc\n")
    }
    assert cs.scan_compose_text("    network_mode: service:db\n    external: false\n") == []


async def test_compose_config_inspects_services_and_risks_without_running_anything(
    tmp_path, daemon
):
    compose(
        tmp_path,
        "services:\n  web:\n    image: nginx\n    privileged: true\n  db:\n    image: postgres\n",
    )
    conn = connector(tmp_path, daemon)
    conn._has_compose = False
    result = await conn.execute("compose_config", {})
    assert result.ok and result.data["services"] == ["web", "db"] and result.data["needs_approval"]
    assert (
        result.data["risks"][0]["scope"] == "docker.container.privileged"
        and result.data["valid"] is None
    )


async def test_compose_up_records_only_the_resources_it_created(tmp_path, daemon, monkeypatch):
    compose(tmp_path, "services:\n  web:\n    image: nginx\n")
    monkeypatch.chdir(tmp_path)
    daemon.add_container(
        "ext", labels={"com.docker.compose.project": tmp_path.name.lower()}
    )  # already running
    conn = connector(tmp_path, daemon)
    conn._has_compose = True

    async def fake_run(args, *, timeout=20.0):
        from velune.resources.base import ResourceResult

        if "up" in args:
            daemon.add_container(
                "compose-web",
                labels={"com.docker.compose.project": args[2], "com.docker.compose.service": "web"},
            )
        return ResourceResult.success(action=args[0], data="")

    monkeypatch.setattr(conn, "_run", fake_run)
    result = await conn.execute("compose_up", {"project": "demo"})
    assert result.ok
    owned = {r.name for r in conn.store.owned("container")}
    assert owned == {"compose-web"}  # the pre-existing project member was not adopted
    project = (await conn.execute("compose_ps", {"project": "demo"})).data["containers"]
    assert project[0]["service"] == "web" and project[0]["ownership"] == "velune"


async def test_compose_down_refuses_a_project_with_containers_velune_did_not_create(
    tmp_path, daemon, monkeypatch
):
    daemon.add_container("theirs", labels={"com.docker.compose.project": "demo"})
    conn = connector(tmp_path, daemon)
    conn._has_compose = True
    ran = []

    async def fake_run(args, *, timeout=20.0):
        from velune.resources.base import ResourceResult

        ran.append(args)
        return ResourceResult.success(action=args[0], data="")

    monkeypatch.setattr(conn, "_run", fake_run)
    result = await conn.execute("compose_down", {"project": "demo"})
    assert (
        not result.ok and "did not create" in result.error and not [a for a in ran if "down" in a]
    )


async def test_compose_down_takes_down_an_owned_project_and_releases_it(
    tmp_path, daemon, monkeypatch
):
    conn = connector(tmp_path, daemon)
    conn._has_compose = True
    from velune.resources.base import ResourceResult

    async def fake_run(args, *, timeout=20.0):
        if "up" in args:
            daemon.add_container("w", labels={"com.docker.compose.project": "demo"})
        if "down" in args:
            daemon.containers.clear()
        return ResourceResult.success(action=args[0], data="")

    monkeypatch.setattr(conn, "_run", fake_run)
    await conn.execute("compose_up", {"project": "demo"})
    assert len(conn.store.owned("container")) == 1
    assert (await conn.execute("compose_down", {"project": "demo"})).ok
    assert conn.store.owned("container") == []


def test_compose_down_and_stop_always_ask_in_auto(tmp_path, daemon):
    conn = connector(tmp_path, daemon)
    assert v(conn, "compose_down", {"project": "demo"}, AUTO, tmp_path) == "ask"
    assert (
        v(conn, "compose_stop", {"project": "demo"}, AUTO, tmp_path) == "ask"
    )  # ownership unknown


# ── Through the discovery tools and the real loop ───────────────────────────


async def test_the_agent_discovers_enables_and_uses_docker_through_a2(tmp_path, daemon):
    conn = connector(tmp_path, daemon)
    asked = []

    async def ask(tool, decision):
        asked.append(decision)
        return Approval.ALLOW_ONCE

    env = make_env(tmp_path, AUTO, connectors={"docker": lambda: conn}, ask=ask)
    provider = ScriptedProvider(
        turn(call("integrations_list", "l1")),
        turn(
            call(
                "integrations_enable",
                "e1",
                integration="docker",
                categories=["inspect", "containers"],
            )
        ),
        turn(
            ToolCall(
                id="c1",
                name="docker__container_create",
                arguments={"image": "nginx:1.27", "name": "web", "ports": ["8080:80"]},
            )
        ),
        turn(call("docker__start", "s1", container="web")),
        turn(
            call("docker__stop", "s2", container="users-db")
        ),  # the user's container: needs a person
        final("done"),
    )
    result = await env.run(provider, "start an nginx container for my project")
    listing = {r["id"]: r for r in json.loads(result.invocations[0].result)["integrations"]}[
        "docker"
    ]
    assert listing["state"] in ("available", "connected") and listing["operations"] == 36
    assert {c["name"] for c in listing["categories"]} == {
        "inspect",
        "containers",
        "images",
        "volumes",
        "networks",
        "compose",
    }
    names = provider.tool_names[2]
    assert (
        "docker__container_create" in names and "docker__image_pull" not in names
    )  # only what was enabled
    assert (
        json.loads(result.invocations[2].result)["ok"]
        and json.loads(result.invocations[3].result)["ok"]
    )
    assert (
        len(asked) == 1 and "users-db" in asked[0].reason
    )  # AUTO asked only for the foreign container


async def test_a_down_daemon_marks_docker_unavailable_and_nothing_else(tmp_path, daemon):
    from tests.http_env import http_connector

    daemon.down = True
    env = make_env(
        tmp_path,
        AUTO,
        connectors={
            "docker": lambda: connector(tmp_path, daemon),
            "http": lambda: http_connector(tmp_path),
        },
    )
    provider = ScriptedProvider(turn(call("integrations_list", "l1")), final("ok"))
    result = await env.run(provider, "check docker")
    rows = {r["id"]: r for r in json.loads(result.invocations[0].result)["integrations"]}
    assert (
        rows["docker"]["state"] == "unavailable"
        and "not running or not reachable" in rows["docker"]["reason"]
    )
    assert rows["http"]["state"] != "unavailable"


async def test_docker_failures_after_enabling_reflect_without_a_ttl_wait(tmp_path, daemon):
    conn = connector(tmp_path, daemon)
    env = make_env(tmp_path, AUTO, connectors={"docker": lambda: conn})
    provider = ScriptedProvider(
        turn(call("integrations_enable", "e1", integration="docker")),
        turn(call("docker__info", "i1")),
        turn(call("integrations_list", "l1")),
        final("ok"),
    )
    daemon_down_after_enable = False

    async def go():
        nonlocal daemon_down_after_enable
        result = await env.run(provider, "inspect")
        return result

    first = await go()
    assert json.loads(first.invocations[1].result)["ok"] is True
    daemon.down = True
    provider2 = ScriptedProvider(
        turn(call("docker__info", "i2")), turn(call("integrations_list", "l2")), final("x")
    )
    second = await env.run(provider2, "again")
    assert json.loads(second.invocations[0].result)["ok"] is False
    rows = {r["id"]: r for r in json.loads(second.invocations[1].result)["integrations"]}
    assert rows["docker"]["state"] == "unavailable" and daemon_down_after_enable is False


def test_no_reflective_operation_exists(tmp_path, daemon):
    actions = {c.action for c in connector(tmp_path, daemon).capabilities()}
    assert not {
        a
        for a in actions
        if any(w in a for w in ("exec", "call", "api", "raw", "run", "eval", "socket"))
    }
    assert "container_exec" not in actions
