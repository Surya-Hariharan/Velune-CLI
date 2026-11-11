"""Resource actions run through the central authorization pipeline (VAIR A1).

Covers: the session PermissionGate deciding /resource actions per execution
mode, the legacy approver wrapped as the gate's ask callback, fail-closed
descriptions, ownership-aware Docker rules (Decision E), structured results,
redaction, lifecycle helpers, and the REPL wiring.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console

from velune.permissions.actions import Action, ActionType, Risk
from velune.permissions.boundary import Boundary
from velune.permissions.gate import Approval, PermissionGate
from velune.permissions.policy import ExecutionMode, PolicyState
from velune.resources.base import (
    ErrorCode,
    Lifecycle,
    ResourceCapability,
    ResourceConnector,
    ResourcePermission,
    ResourceResult,
    ResourceState,
    ResourceStatus,
)
from velune.resources.manager import ResourceManager
from velune.resources.ownership import Ownership, OwnershipLedger, ResourceRef
from velune.tools.base.tool import ToolCallContext
from velune.tools.safety import ApprovalMode

# ── Fixtures ────────────────────────────────────────────────────────────────


class Recorder(ResourceConnector):
    resource_id = "fake"
    display_name = "Fake"

    def __init__(self, name=None):
        super().__init__(name)
        self.ran: list[str] = []
        self.state = ResourceState.DISCONNECTED
        self.connects = 0

    async def connect(self):
        self.connects += 1
        await asyncio.sleep(0.01)
        self.state = ResourceState.CONNECTED
        return ResourceResult.success("connect")

    async def disconnect(self):
        self.state = ResourceState.DISCONNECTED
        return ResourceResult.success("disconnect")

    def status(self):
        return ResourceStatus(self.resource_id, self.display_name, self.state)

    async def discover(self):
        return []

    def capabilities(self):
        return [
            ResourceCapability("peek", ResourcePermission.READ, "read", scope="fake.item.read"),
            ResourceCapability("poke", ResourcePermission.WRITE, "write", scope="fake.item.write"),
            ResourceCapability("run", ResourcePermission.EXECUTE, "exec"),
            ResourceCapability(
                "wipe", ResourcePermission.ADMIN, "wipe", destructive=True, scope="fake.item.delete"
            ),
        ]

    async def execute(self, action, params=None):
        self.ran.append(action)
        if action == "peek":
            return ResourceResult.success(action, {"password": "hunter2-long", "rows": 1})
        return ResourceResult.success(action, "done")


def _manager(tmp_path, *, mode=ExecutionMode.MANUAL, ask=None, records=None, **kw):
    records = records if records is not None else []

    def factory():
        gate = PermissionGate(
            state=PolicyState(mode=mode),
            boundary=Boundary(tmp_path),
            ask=ask,
            audit=records.append,
        )
        return ToolCallContext(run_id="t", actor="test", workspace=tmp_path, gate=gate)

    m = ResourceManager(workspace=tmp_path, context_factory=factory, **kw)
    m.register("fake", Recorder)
    return m, records


# ── Mode semantics through the session gate ─────────────────────────────────


@pytest.mark.parametrize("mode", list(ExecutionMode))
async def test_reads_run_in_every_mode(tmp_path, mode):
    m, _ = _manager(tmp_path, mode=mode)
    result = await m.execute("fake", "peek")
    assert result.ok


async def test_plan_mode_refuses_changes_without_running_them(tmp_path):
    m, records = _manager(tmp_path, mode=ExecutionMode.PLAN)
    result = await m.execute("fake", "poke")
    assert not result.ok and result.denied
    assert result.code is ErrorCode.AUTHORIZATION_DENIED
    assert m.get("fake").ran == []
    assert records[-1]["decision"] == "deny"
    assert records[-1]["actions"][0]["scope"] == "fake.item.write"


async def test_auto_mode_runs_medium_changes_and_audits_them(tmp_path):
    m, records = _manager(tmp_path, mode=ExecutionMode.AUTO)
    assert (await m.execute("fake", "poke")).ok
    assert (await m.execute("fake", "run")).ok
    assert records[-1]["actions"][0]["scope"] == "fake.run"  # default <id>.<action>
    assert records[-1]["actions"][0]["type"] == "INTEGRATION_EXECUTE"


async def test_auto_mode_still_asks_for_destructive_actions(tmp_path):
    m, _ = _manager(tmp_path, mode=ExecutionMode.AUTO)  # no ask callback → denial
    result = await m.execute("fake", "wipe")
    assert result.denied and m.get("fake").ran == []


async def test_manual_mode_asks_and_honours_the_answer(tmp_path):
    answers = [Approval.DENY, Approval.ALLOW_ONCE]

    async def ask(tool, decision):
        return answers.pop(0)

    m, _ = _manager(tmp_path, ask=ask)
    assert (await m.execute("fake", "poke")).denied
    assert (await m.execute("fake", "poke")).ok
    assert m.get("fake").ran == ["poke"]


async def test_missing_context_fails_closed_for_changes(tmp_path):
    m = ResourceManager(workspace=tmp_path, context_factory=lambda: None)
    m.register("fake", Recorder)
    assert (await m.execute("fake", "peek")).ok  # reads need no gate
    assert (await m.execute("fake", "poke")).denied


async def test_crashing_context_factory_fails_closed(tmp_path):
    def boom():
        raise RuntimeError("no session")

    m = ResourceManager(workspace=tmp_path, context_factory=boom)
    m.register("fake", Recorder)
    assert (await m.execute("fake", "poke")).denied


# ── Legacy approver: consulted only when the policy asks ────────────────────


async def test_legacy_approver_is_not_consulted_for_reads(tmp_path):
    calls: list[Any] = []

    async def approver(req):
        calls.append(req)
        return True

    m = ResourceManager(workspace=tmp_path, approver=approver)
    m.register("fake", Recorder)
    assert (await m.execute("fake", "peek")).ok
    assert calls == []
    assert (await m.execute("fake", "poke")).ok
    assert calls[0].permission is ResourcePermission.WRITE


# ── Fail-closed descriptions and no weakening ───────────────────────────────


class Unscoped(Recorder):
    def describe_actions(self, action, params, *, ledger=None):
        return [Action(ActionType.INTEGRATION_WRITE, "x", scope="")]


class Lying(Recorder):
    """Claims a write is a read; the framework's baseline must still apply."""

    def describe_actions(self, action, params, *, ledger=None):
        return [Action(ActionType.INTEGRATION_READ, "x", scope="fake.item.read")]


class Exploding(Recorder):
    def describe_actions(self, action, params, *, ledger=None):
        raise RuntimeError("cannot describe")


@pytest.mark.parametrize("cls", [Unscoped, Exploding])
async def test_undescribable_operation_is_refused_even_in_auto(tmp_path, cls):
    m, _ = _manager(tmp_path, mode=ExecutionMode.AUTO)
    m.register("fake", cls)
    result = await m.execute("fake", "poke")
    assert result.denied and m.get("fake").ran == []


async def test_connector_cannot_downgrade_a_write_to_a_read(tmp_path):
    m, records = _manager(tmp_path, mode=ExecutionMode.PLAN)
    m.register("fake", Lying)
    result = await m.execute("fake", "poke")
    assert result.denied and m.get("fake").ran == []
    types = {a["type"] for a in records[-1]["actions"]}
    assert "INTEGRATION_WRITE" in types


async def test_path_outside_the_workspace_is_refused_in_auto(tmp_path):
    m, _ = _manager(tmp_path / "ws", mode=ExecutionMode.AUTO)
    (tmp_path / "ws").mkdir()
    result = await m.execute("fake", "poke", {"path": str(tmp_path)})
    assert result.denied
    assert (await m.execute("fake", "poke", {"path": str(tmp_path / "ws" / "sub")})).ok


async def test_hook_block_is_a_denial(tmp_path):
    class BlockingHooks:
        async def dispatch_pre_tool_use(self, **kw):
            return SimpleNamespace(blocked=True, block_reason="policy", system_message="")

        async def dispatch_post_tool_use(self, **kw):
            return None

    def factory():
        gate = PermissionGate(
            state=PolicyState(mode=ExecutionMode.AUTO), boundary=Boundary(tmp_path)
        )
        return ToolCallContext(
            run_id="t", actor="t", workspace=tmp_path, gate=gate, hook_dispatcher=BlockingHooks()
        )

    m = ResourceManager(workspace=tmp_path, context_factory=factory)
    m.register("fake", Recorder)
    result = await m.execute("fake", "poke")
    assert result.denied and "hook" in result.error and m.get("fake").ran == []


# ── Structured results and redaction ────────────────────────────────────────


async def test_results_are_structured_and_redacted(tmp_path):
    m, _ = _manager(tmp_path)
    result = await m.execute("fake", "peek")
    assert result.integration == "fake"
    assert result.data["password"] == "***REDACTED***"
    assert result.data["rows"] == 1
    assert result.duration_ms >= 0
    payload = result.to_dict()
    assert payload["ok"] is True and "hunter2" not in str(payload)


async def test_errors_carry_normalized_codes(tmp_path):
    m, _ = _manager(tmp_path)
    unknown = await m.execute("fake", "nope")
    assert unknown.code is ErrorCode.UNSUPPORTED_OPERATION
    assert unknown.to_dict()["error"]["code"] == "unsupported_operation"
    missing = await m.execute("ghost", "peek")
    assert missing.code is ErrorCode.UNAVAILABLE


async def test_connector_exception_is_redacted_and_normalized(tmp_path):
    class Leaky(Recorder):
        async def execute(self, action, params=None):
            raise RuntimeError("connect failed: postgres://app:s3cr3t-pass@db:5432/x")

    m, _ = _manager(tmp_path, mode=ExecutionMode.AUTO)
    m.register("fake", Leaky)
    result = await m.execute("fake", "poke")
    assert result.code is ErrorCode.ADAPTER_FAILURE
    assert "s3cr3t-pass" not in result.error


# ── Lifecycle foundation ────────────────────────────────────────────────────


async def test_ensure_connected_shares_one_attempt_under_concurrency(tmp_path):
    class Persistent(Recorder):
        lifecycle = Lifecycle.PERSISTENT

    m, _ = _manager(tmp_path)
    m.register("fake", Persistent)
    results = await asyncio.gather(*(m.ensure_connected("fake") for _ in range(5)))
    assert all(r.ok for r in results)
    # One connection attempt; every other caller reuses it.
    assert m.get("fake").connects == 1
    assert await m.health("fake")


async def test_ensure_connected_is_a_noop_for_stateless_connectors(tmp_path):
    m, _ = _manager(tmp_path)
    assert (await m.ensure_connected("fake")).ok
    assert m.get("fake").connects == 0


# ── Ownership ledger ────────────────────────────────────────────────────────


def test_ledger_distinguishes_owned_preexisting_and_unknown():
    ledger = OwnershipLedger("s1")
    mine = ResourceRef("docker", "container", "api")
    theirs = ResourceRef("docker", "container", "db")
    ledger.record_created(mine)
    ledger.record_preexisting([theirs, mine])  # a snapshot never downgrades OWNED
    assert ledger.ownership(mine) is Ownership.OWNED
    assert ledger.ownership(theirs) is Ownership.PREEXISTING
    assert ledger.ownership(ResourceRef("docker", "container", "x")) is Ownership.UNKNOWN
    assert ledger.owned("docker") == [mine]


def test_ledger_round_trip_does_not_widen_ownership_across_sessions():
    ledger = OwnershipLedger("s1")
    ref = ResourceRef("docker", "container", "api")
    ledger.record_created(ref)
    same = OwnershipLedger.from_dict(ledger.to_dict())
    assert same.is_owned(ref)
    other = OwnershipLedger.from_dict(ledger.to_dict(), session_id="s2")
    assert other.ownership(ref) is Ownership.PREEXISTING


# ── Docker: Decision E ──────────────────────────────────────────────────────


def _docker_manager(tmp_path, mode, records=None):
    from tests.docker_fakes import FakeDaemon, make_engine
    from velune.resources.docker.connector import DockerConnector

    m, records = _manager(tmp_path, mode=mode, records=records)
    ran: list[list[str]] = []
    daemon = FakeDaemon()
    daemon.add_image("nginx:1.27")
    daemon.add_container("users-db")  # a container the user already had

    class FakeDocker(DockerConnector):
        @staticmethod
        def _docker_path():
            return "docker"

        async def _run(self, args, *, timeout=20.0):
            ran.append(args)
            if args[0] == "build":
                daemon.add_image(args[2])  # the build produces a new image
            return ResourceResult.success(action=args[0], data="")

    m.register(
        "docker",
        lambda: FakeDocker(
            engine=make_engine(daemon), workspace=tmp_path, workspace_trusted=True, session_id="s1"
        ),
    )
    m.daemon = daemon
    return m, records, ran


async def test_auto_cannot_stop_a_container_it_did_not_create(tmp_path):
    m, records, ran = _docker_manager(tmp_path, ExecutionMode.AUTO)
    result = await m.execute("docker", "stop", {"container": "users-db"})
    assert result.denied and m.daemon.calls == []
    assert records[-1]["actions"][0]["risk"] == "high"
    assert records[-1]["actions"][0]["scope"] == "docker.container.stop"


async def test_auto_may_manage_a_container_the_task_owns(tmp_path):
    m, _, _ = _docker_manager(tmp_path, ExecutionMode.AUTO)
    made = await m.execute("docker", "container_create", {"image": "nginx:1.27", "name": "velune-api"})
    assert made.ok
    for action in ("start", "stop", "restart"):
        assert (await m.execute("docker", action, {"container": "velune-api"})).ok
    assert [c[0] for c in m.daemon.calls] == ["running", "exited", "running"]


async def test_preexisting_container_is_never_assumed_owned(tmp_path):
    m, _, _ = _docker_manager(tmp_path, ExecutionMode.AUTO)
    assert (await m.execute("docker", "restart", {"container": "users-db"})).denied
    assert m.daemon.calls == []


@pytest.mark.parametrize("mode", [ExecutionMode.AUTO, ExecutionMode.MANUAL])
async def test_destructive_compose_and_volume_deletion_need_a_human(tmp_path, mode):
    m, records, ran = _docker_manager(tmp_path, mode)
    m.get("docker")._has_compose = True
    result = await m.execute("docker", "compose_down", {"volumes": True})
    assert result.denied and ran == []
    scopes = {a["scope"] for a in records[-1]["actions"]}
    assert {"docker.compose.down", "docker.volume.delete"} <= scopes


async def test_docker_reads_run_in_plan_mode(tmp_path):
    m, _, _ = _docker_manager(tmp_path, ExecutionMode.PLAN)
    assert (await m.execute("docker", "ps")).ok
    assert (await m.execute("docker", "logs", {"container": "users-db"})).ok
    assert (await m.execute("docker", "build", {"path": "."})).denied


async def test_build_records_the_image_as_owned(tmp_path):
    m, _, _ = _docker_manager(tmp_path, ExecutionMode.AUTO)
    result = await m.execute("docker", "build", {"path": ".", "tag": "velune/app:dev"})
    assert result.ok and result.resource_ids
    image_id = next(a["Id"] for a in m.daemon._images.values() if "velune/app:dev" in a["RepoTags"])
    assert m.ledger.is_owned(ResourceRef("docker", "image", image_id))  # by id, not by tag


async def test_build_context_outside_the_workspace_is_refused_in_auto(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    m, _, ran = _docker_manager(ws, ExecutionMode.AUTO)
    assert (await m.execute("docker", "build", {"path": str(tmp_path)})).denied
    assert ran == []


def test_sql_query_scope_follows_the_statement():
    from velune.resources.postgres.connector import PostgresConnector

    c = PostgresConnector(config={"host": "localhost"})
    scopes = {
        sql: c.describe_actions("query", {"sql": sql})[0].scope
        for sql in ("SELECT 1", "UPDATE t SET a=1", "DROP TABLE t")
    }
    assert scopes == {
        "SELECT 1": "database.query.read",
        "UPDATE t SET a=1": "database.query.write",
        "DROP TABLE t": "database.query.destructive",
    }
    drop = c.describe_actions("query", {"sql": "DROP TABLE t"})[0]
    assert drop.action_type is ActionType.INTEGRATION_DELETE and drop.risk is Risk.HIGH
    assert c.describe_actions("list_tables", {})[0].scope == "database.schema.read"


# ── REPL wiring: /resource uses the session's PermissionGate ────────────────


class _Container:
    def __init__(self, services):
        self._s = services

    def get(self, key):
        if key not in self._s:
            raise KeyError(key)
        return self._s[key]

    def has(self, key):
        return key in self._s


def _fake_repl(tmp_path: Path, mode: ExecutionMode, approval=ApprovalMode.ASK):
    return SimpleNamespace(
        console=Console(file=io.StringIO(), force_terminal=False, width=100),
        container=_Container({"runtime.workspace": str(tmp_path)}),
        _execution_mode=mode,
        _approval_mode=approval,
        _tool_session_grants=set(),
        _hook_dispatcher=None,
        _session_id="sess",
        _episodic_session_id=None,
    )


async def test_repl_resource_actions_follow_the_session_mode(tmp_path):
    from velune.cli.handlers.resources import make_resource_context_factory

    repl = _fake_repl(tmp_path, ExecutionMode.PLAN)
    m = ResourceManager(workspace=tmp_path, context_factory=make_resource_context_factory(repl))
    m.register("fake", Recorder)
    assert (await m.execute("fake", "peek")).ok
    assert (await m.execute("fake", "poke")).denied

    repl._execution_mode = ExecutionMode.AUTO
    assert (await m.execute("fake", "poke")).ok
    # The same session gate object serves the agent's tools and /resource.
    assert repl._permission_gate is not None


async def test_approve_block_refuses_changes_because_it_selects_plan_mode(tmp_path):
    """`/approve block` maps to PLAN (see handlers/settings.py); no special case is needed."""
    from velune.cli.handlers.resources import make_resource_context_factory
    from velune.cli.handlers.settings import cmd_approve

    repl = _fake_repl(tmp_path, ExecutionMode.AUTO)
    repl._status_state = SimpleNamespace(execution_label="")
    repl._plan_manager = None
    await cmd_approve(repl, "block")
    assert repl._execution_mode is ExecutionMode.PLAN

    m = ResourceManager(workspace=tmp_path, context_factory=make_resource_context_factory(repl))
    m.register("fake", Recorder)
    assert (await m.execute("fake", "peek")).ok
    assert (await m.execute("fake", "poke")).denied
    assert m.get("fake").ran == ["peek"]


def test_repl_builds_the_manager_with_the_session_context(tmp_path, monkeypatch):
    """`_build_resource_manager` installs the gate-backed context factory."""
    from velune.cli.repl import VeluneREPL

    fake = SimpleNamespace(container=_Container({}))
    manager = VeluneREPL._build_resource_manager(fake, str(tmp_path))
    assert manager._context_factory is not None
