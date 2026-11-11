"""The Docker connector against an in-memory daemon: operations, ownership, cleanup (VAIR A4)."""

from __future__ import annotations

import json

import pytest

from tests.docker_fakes import FakeDaemon, make_engine
from velune.resources.base import ErrorCode, ResourceState
from velune.resources.docker.connector import DockerConnector
from velune.resources.docker.ownership import LABEL_MANAGED


@pytest.fixture
def daemon():
    d = FakeDaemon()
    d.add_image("nginx:1.27")
    return d


def make(tmp_path, daemon, **kw):
    return DockerConnector(
        engine=make_engine(daemon),
        workspace=tmp_path,
        workspace_trusted=True,
        session_id=kw.pop("session", "s1"),
        **kw,
    )


async def ok(conn, action, **params):
    result = await conn.execute(action, params)
    assert result.ok, f"{action}: {result.error}"
    return result


# ── Connecting ──────────────────────────────────────────────────────────────


async def test_connect_reports_the_engine_and_state(tmp_path, daemon):
    conn = make(tmp_path, daemon)
    result = await conn.connect()
    assert result.ok and result.data["version"] == "27.0.1" and result.data["api_version"] == "1.45"
    status = conn.status()
    assert (
        status.state is ResourceState.CONNECTED
        and "27.0.1" in status.detail
        and not status.info["endpoint"].endswith("key")
    )


async def test_a_down_daemon_is_unavailable_with_a_hint_not_a_crash(tmp_path, daemon):
    daemon.down = True
    conn = make(tmp_path, daemon)
    result = await conn.connect()
    assert not result.ok and result.code is ErrorCode.UNAVAILABLE and "Start Docker" in result.hint
    assert conn.status().state is ResourceState.ERROR
    assert (await conn.probe_availability())[0] is False
    assert [h.source for h in await conn.discover()] in ([], ["cli"])


async def test_disconnect_closes_the_client_and_a_restart_reconnects(tmp_path, daemon):
    conn = make(tmp_path, daemon)
    await conn.connect()
    await conn.disconnect()
    assert daemon.clients_closed == 1 and conn.status().state is ResourceState.DISCONNECTED
    assert (await conn.execute("info", {})).ok  # lazily reconnects
    assert daemon.clients_opened == 2


async def test_a_daemon_that_dies_mid_session_is_reported_and_then_recovers(tmp_path, daemon):
    conn = make(tmp_path, daemon)
    await ok(conn, "ps")
    daemon.down = True
    failed = await conn.execute("start", {"container": "x"})
    assert not failed.ok and failed.code is ErrorCode.UNAVAILABLE
    assert await conn.health() is False and conn.status().state is ResourceState.ERROR
    daemon.down = False
    assert (await conn.execute("ps", {})).ok and conn.status().state is ResourceState.CONNECTED


# ── Inspecting ──────────────────────────────────────────────────────────────


async def test_ps_marks_who_created_each_container(tmp_path, daemon):
    daemon.add_container("users-db")
    conn = make(tmp_path, daemon)
    await ok(conn, "container_create", image="nginx:1.27", name="web")
    rows = {r["name"]: r for r in (await ok(conn, "ps", all=True)).data}
    assert rows["web"]["ownership"] == "velune" and rows["users-db"]["ownership"] == "preexisting"


async def test_inspect_shows_environment_names_never_values(tmp_path, daemon):
    daemon.add_container("api", env=["DB_PASSWORD=hunter2hunter2", "MODE=prod"])
    conn = make(tmp_path, daemon)
    result = await ok(conn, "inspect", target="api")
    assert result.data["environment_variables"] == ["DB_PASSWORD", "MODE"]
    assert "hunter2hunter2" not in json.dumps(result.data)
    image = await ok(conn, "image_inspect", image="nginx:1.27")
    assert "SECRET" in image.data["environment_variables"] and "hunter2hunter2" not in json.dumps(
        image.data
    )


async def test_logs_are_bounded_and_redacted(tmp_path, daemon):
    body = "\n".join(f"line {i} password=abcd1234efgh5678" for i in range(2000)).encode()
    daemon.add_container("noisy", logs=body)
    conn = make(tmp_path, daemon, config={"max_log_lines": 50, "max_output_chars": 2000})
    result = await ok(conn, "logs", container="noisy", tail=5000)
    assert result.data["lines"] == 50 and result.data["truncated"] is True
    assert len(result.data["text"]) <= 2000 and "abcd1234efgh5678" not in result.data["text"]
    assert "line 1999" in result.data["text"]  # the recent end is kept


async def test_health_stats_and_info(tmp_path, daemon):
    daemon.add_container("svc")
    conn = make(tmp_path, daemon)
    assert (await ok(conn, "health", container="svc")).data["health"] == "none"
    stats = (await ok(conn, "stats", container="svc")).data
    assert stats["cpu_percent"] == 20.0 and stats["memory_usage_mb"] == 50.0 and stats["pids"] == 3
    info = (await ok(conn, "info")).data
    assert info["cpus"] == 4 and info["remote"] is False


async def test_a_missing_container_is_not_found(tmp_path, daemon):
    result = await make(tmp_path, daemon).execute("logs", {"container": "ghost"})
    assert not result.ok and result.code is ErrorCode.NOT_FOUND


@pytest.mark.parametrize("action", ["start", "logs", "inspect", "health", "container_remove"])
@pytest.mark.parametrize("bad", ["--help", "-f", "a b", "../x", "a;b"])
async def test_option_like_or_malformed_references_never_reach_the_daemon(
    tmp_path, daemon, action, bad
):
    params = {"target": bad} if action == "inspect" else {"container": bad}
    result = await make(tmp_path, daemon).execute(action, params)
    assert not result.ok and result.code is ErrorCode.VALIDATION_FAILED and daemon.calls == []


# ── Creating and managing containers ────────────────────────────────────────


async def test_create_validates_labels_and_records_ownership(tmp_path, daemon):
    conn = make(tmp_path, daemon)
    result = await ok(
        conn,
        "container_create",
        image="nginx:1.27",
        name="web",
        command="nginx -g 'daemon off;'",
        env=["A=1"],
        ports=["8080:80"],
        restart_policy="unless-stopped",
        memory="256m",
        cpus=0.5,
        labels=["team=core"],
    )
    kwargs = daemon.created_kwargs[-1]
    assert kwargs["command"] == ["nginx", "-g", "daemon off;"] and kwargs["environment"] == {
        "A": "1"
    }
    assert kwargs["ports"] == {"80/tcp": ("127.0.0.1", 8080)}  # loopback unless asked otherwise
    assert (
        kwargs["restart_policy"] == {"Name": "unless-stopped"}
        and kwargs["mem_limit"] == 256 * 1024**2
    )
    assert kwargs["nano_cpus"] == 500_000_000 and kwargs["security_opt"] == [
        "no-new-privileges:true"
    ]
    assert kwargs["labels"]["team"] == "core" and kwargs["labels"][LABEL_MANAGED] == "true"
    assert result.data["ownership"] == "velune" and result.resource_ids
    assert conn.store.owned("container")[0].name == "web"


@pytest.mark.parametrize(
    "params",
    [
        {"image": "--rm"},
        {"image": "nginx:1.27", "name": "-x"},
        {"image": "nginx:1.27", "ports": ["bad"]},
        {"image": "nginx:1.27", "env": ["bad"]},
        {"image": "nginx:1.27", "volumes": ["no-target"]},
        {"image": "nginx:1.27", "labels": ["dev.velune.managed=true"]},
        {"image": "nginx:1.27", "memory": "lots"},
        {"image": "nginx:1.27", "restart_policy": "sometimes"},
        {"image": "nginx:1.27", "devices": ["/etc/shadow"]},
    ],
)
async def test_invalid_creation_arguments_fail_before_the_daemon_is_touched(
    tmp_path, daemon, params
):
    result = await make(tmp_path, daemon).execute("container_create", params)
    assert (
        not result.ok and result.code is ErrorCode.VALIDATION_FAILED and daemon.created_kwargs == []
    )


async def test_a_name_collision_never_makes_the_existing_container_owned(tmp_path, daemon):
    daemon.add_container("web")
    conn = make(tmp_path, daemon)
    result = await conn.execute("container_create", {"image": "nginx:1.27", "name": "web"})
    assert not result.ok and result.code is ErrorCode.CONFLICT and "not Velune's" in result.error
    assert conn.store.owned("container") == [] and daemon.created_kwargs == []


async def test_creating_from_a_missing_image_points_to_pull(tmp_path, daemon):
    result = await make(tmp_path, daemon).execute("container_create", {"image": "redis:7"})
    assert not result.ok and result.code is ErrorCode.NOT_FOUND and "image_pull" in result.hint


async def test_privileged_options_are_passed_only_when_asked_for(tmp_path, daemon):
    conn = make(tmp_path, daemon)
    await ok(conn, "container_create", image="nginx:1.27", name="plain")
    plain = daemon.created_kwargs[-1]
    assert not {"privileged", "network_mode", "devices", "cap_add"} & set(plain)
    await ok(
        conn,
        "container_create",
        image="nginx:1.27",
        name="host",
        host_network=True,
        ports=["80:80"],
    )
    assert (
        daemon.created_kwargs[-1]["network_mode"] == "host"
        and "ports" not in daemon.created_kwargs[-1]
    )


async def test_lifecycle_acts_on_the_judged_id_and_reports_state(tmp_path, daemon):
    conn = make(tmp_path, daemon)
    await ok(conn, "container_create", image="nginx:1.27", name="web")
    assert (await ok(conn, "start", container="web")).data["status"] == "running"
    assert (await ok(conn, "pause", container="web")).data["status"] == "paused"
    assert (await ok(conn, "unpause", container="web")).data["status"] == "running"
    assert (await ok(conn, "restart", container="web", timeout=1)).data["status"] == "running"
    assert (await ok(conn, "stop", container="web")).data["status"] == "exited"
    assert all(len(c[1]) == 64 for c in daemon.calls)  # operations used the id, not the name


async def test_a_name_re_pointed_after_authorization_is_refused(tmp_path, daemon):
    """The authorized container is removed and a user's container takes its name."""
    conn = make(tmp_path, daemon)
    await ok(conn, "container_create", image="nginx:1.27", name="web")
    conn.describe_actions("stop", {"container": "web"})  # AUTO authorizes: it is Velune's
    old_id = next(iter(daemon.containers))
    daemon.containers.pop(old_id)
    daemon.add_container("web")  # a different, pre-existing-looking container with the same name
    result = await conn.execute("stop", {"container": "web"})
    assert not result.ok and result.code is ErrorCode.AUTHORIZATION_DENIED and daemon.calls == []


async def test_removing_a_container_releases_its_ownership(tmp_path, daemon):
    conn = make(tmp_path, daemon)
    await ok(conn, "container_create", image="nginx:1.27", name="web")
    result = await ok(conn, "container_remove", container="web")
    assert (
        result.data["removed"] == "web"
        and conn.store.owned("container") == []
        and daemon.containers == {}
    )
    refused = await make(tmp_path, daemon).execute("container_remove", {"container": "gone"})
    assert refused.code is ErrorCode.NOT_FOUND


async def test_wait_ready_uses_health_logs_or_a_steady_running_state(tmp_path, daemon, monkeypatch):
    async def instant(_):
        return None

    monkeypatch.setattr("velune.resources.docker.connector.asyncio.sleep", instant)
    attrs = daemon.add_container("svc", logs=b"booting\nlistening on :80\n")
    conn = make(tmp_path, daemon)
    by_log = await ok(conn, "wait_ready", container="svc", log_contains="listening on", timeout=5)
    assert by_log.data["ready"] and "output appeared" in by_log.data["reason"]
    steady = await ok(conn, "wait_ready", container="svc", timeout=5)
    assert steady.data["ready"] and "no health check" in steady.data["reason"]
    attrs["State"]["Health"] = {"Status": "starting"}
    assert (await ok(conn, "wait_ready", container="svc", timeout=1)).data["ready"] is False
    attrs["State"]["Health"] = {"Status": "healthy"}
    assert (await ok(conn, "wait_ready", container="svc", timeout=5)).data["reason"] == "healthy"
    attrs["State"].update(Status="exited", Running=False, ExitCode=3, Health=None)
    stopped = await ok(conn, "wait_ready", container="svc", timeout=5)
    assert stopped.data["ready"] is False and "exit code 3" in stopped.data["reason"]


# ── Images ──────────────────────────────────────────────────────────────────


async def test_a_new_image_pull_is_owned_by_id(tmp_path, daemon):
    conn = make(tmp_path, daemon)
    result = await ok(conn, "image_pull", image="redis:7")
    assert not result.data["already_present"] and len(conn.store.owned("image")) == 1
    assert result.resource_ids


async def test_pulling_an_existing_tag_never_assumes_ownership(tmp_path, daemon):
    conn = make(tmp_path, daemon)
    result = await ok(conn, "image_pull", image="nginx:1.27")
    assert result.data["already_present"] and conn.store.owned("image") == []
    assert any("not treated as created" in w for w in result.warnings)


async def test_tagging_refuses_to_move_an_existing_tag(tmp_path, daemon):
    other = daemon.add_image("app:stable")
    conn = make(tmp_path, daemon)
    clash = await conn.execute(
        "image_tag", {"image": "nginx:1.27", "repository": "app", "tag": "stable"}
    )
    assert not clash.ok and clash.code is ErrorCode.CONFLICT and other["RepoTags"] == ["app:stable"]
    fresh = await ok(conn, "image_tag", image="nginx:1.27", repository="app", tag="v2")
    assert fresh.data["tagged"] == "app:v2"
    assert conn.store.owned("image") == []  # a tag does not make the image Velune's


async def test_image_removal_untags_or_deletes_and_releases_ownership(tmp_path, daemon):
    conn = make(tmp_path, daemon)
    await ok(conn, "image_pull", image="redis:7")
    await ok(conn, "image_remove", image="redis:7")
    assert conn.store.owned("image") == []
    assert (await conn.execute("image_remove", {"image": "redis:7"})).code is ErrorCode.NOT_FOUND


# ── Volumes and networks ────────────────────────────────────────────────────


async def test_volume_lifecycle_with_usage_and_collisions(tmp_path, daemon):
    daemon.add_volume("user-data")
    conn = make(tmp_path, daemon)
    clash = await conn.execute("volume_create", {"name": "user-data"})
    assert clash.code is ErrorCode.CONFLICT and conn.store.owned("volume") == []
    await ok(conn, "volume_create", name="scratch")
    await ok(conn, "container_create", image="nginx:1.27", name="w", volumes=["scratch:/data"])
    rows = {r["name"]: r for r in (await ok(conn, "volume_ls")).data}
    assert rows["scratch"]["in_use"] and rows["scratch"]["attached_containers"] == ["w"]
    assert (
        rows["scratch"]["ownership"] == "velune" and rows["user-data"]["ownership"] == "preexisting"
    )
    busy = await conn.execute("volume_remove", {"name": "scratch"})
    assert busy.code is ErrorCode.CONFLICT and "in use by: w" in busy.error
    await ok(conn, "container_remove", container="w")
    await ok(conn, "volume_remove", name="scratch")
    assert "scratch" not in daemon.volumes and conn.store.owned("volume") == []
    assert (await ok(conn, "volume_inspect", volume="user-data")).data["in_use"] is False


async def test_network_lifecycle_membership_and_protection(tmp_path, daemon):
    daemon.add_network("corp-net")
    conn = make(tmp_path, daemon)
    assert (await conn.execute("network_create", {"name": "corp-net"})).code is ErrorCode.CONFLICT
    assert (
        await conn.execute("network_create", {"name": "bridge"})
    ).code is ErrorCode.VALIDATION_FAILED
    await ok(conn, "network_create", name="app-net", internal=True)
    await ok(conn, "container_create", image="nginx:1.27", name="w")
    await ok(conn, "network_connect", network="app-net", container="w")
    detail = (await ok(conn, "network_inspect", network="app-net")).data
    assert (
        detail["attached_containers"] == ["w"]
        and detail["internal"]
        and detail["ownership"] == "velune"
    )
    busy = await conn.execute("network_remove", {"network": "app-net"})
    assert busy.code is ErrorCode.CONFLICT
    await ok(conn, "network_disconnect", network="app-net", container="w")
    await ok(conn, "network_remove", network="app-net")
    for builtin in ("bridge", "host", "none"):
        assert (
            await conn.execute("network_remove", {"network": builtin})
        ).code is ErrorCode.VALIDATION_FAILED
    names = {n["name"]: n["ownership"] for n in (await ok(conn, "network_ls")).data}
    assert names["corp-net"] == "preexisting" and "app-net" not in names


# ── Ownership across restarts and reconciliation ────────────────────────────


async def test_ownership_survives_a_restart_as_previous_session_and_needs_approval(
    tmp_path, daemon
):
    from velune.resources.docker.ownership import DockerOwnershipStore

    path = tmp_path / "own.json"
    first = DockerConnector(
        engine=make_engine(daemon),
        workspace=tmp_path,
        workspace_trusted=True,
        store=DockerOwnershipStore("s1", tmp_path, path=path),
    )
    await ok(first, "container_create", image="nginx:1.27", name="web")
    second = DockerConnector(
        engine=make_engine(daemon),
        workspace=tmp_path,
        workspace_trusted=True,
        store=DockerOwnershipStore("s2", tmp_path, path=path),
    )
    rows = {r["name"]: r for r in (await ok(second, "ps", all=True)).data}
    assert rows["web"]["ownership"] == "velune_previous"
    actions = second.describe_actions("stop", {"container": "web"})
    assert any(a.risk.value == "high" for a in actions)  # an earlier session's container still asks


async def test_connect_reconciles_records_against_the_daemon(tmp_path, daemon):
    conn = make(tmp_path, daemon)
    await ok(conn, "container_create", image="nginx:1.27", name="web")
    daemon.containers.clear()  # removed behind Velune's back
    await conn.disconnect()
    await conn.connect()
    assert conn.store.owned("container") == []


async def test_a_container_stripped_of_its_labels_is_no_longer_trusted(tmp_path, daemon):
    conn = make(tmp_path, daemon)
    await ok(conn, "container_create", image="nginx:1.27", name="web")
    next(iter(daemon.containers.values()))["Config"]["Labels"].clear()
    assert (await ok(conn, "ps", all=True)).data[0]["ownership"] == "unknown"


# ── Output bounds and curation ──────────────────────────────────────────────


async def test_large_listings_are_bounded(tmp_path, daemon):
    for i in range(700):
        daemon.add_container(f"c{i}")
    assert len((await ok(make(tmp_path, daemon), "ps", all=True)).data) == 500
