"""Capability discovery: manifests, schemas, availability, selection, scope (VAIR A2)."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.integration_fakes import FakeApp, OtherApp, make_env, many_ops_app
from velune.permissions.actions import valid_scope
from velune.resources.base import (
    DiscoveryHint,
    ResourceCapability,
    ResourceConnector,
    ResourcePermission,
    ResourceResult,
    ResourceState,
    ResourceStatus,
)
from velune.resources.capabilities import (
    CapabilityCatalog,
    CapabilityScope,
    ManifestError,
    build_manifest,
    compact_payload,
    model_schema,
    validate_arguments,
)
from velune.resources.manager import ResourceManager

# ── Manifests derive from the real connectors ───────────────────────────────


@pytest.mark.parametrize("rid", ["docker", "postgres", "mysql", "supabase"])
def test_real_connectors_produce_valid_manifests(rid):
    manager = ResourceManager()
    manager.register_defaults()
    manifest = build_manifest(manager.get(rid))
    assert manifest.id == rid and manifest.description and manifest.operations
    for op in manifest.operations:
        assert op.tool_name == f"{rid}__{op.action}" and len(op.tool_name) <= 64
        assert valid_scope(op.scope)
        assert op.schema["type"] == "object"


def test_process_connector_manifest():
    from velune.core.task_registry import JobRegistry

    manager = ResourceManager(job_registry=JobRegistry())
    manager.register_defaults()
    m = build_manifest(manager.get("process"))
    assert {o.action for o in m.operations} == {"list", "status", "logs", "wait_ready", "stop"}
    assert m.operation("stop").permission == "write" and m.operation("logs").read_only


def test_sql_query_scope_is_the_declared_read_scope():
    manager = ResourceManager()
    manager.register_defaults()
    op = build_manifest(manager.get("postgres")).operation("query")
    assert op.scope == "database.query.read" and op.read_only


def test_docker_categories_group_its_operations():
    manager = ResourceManager()
    manager.register_defaults()
    m = build_manifest(manager.get("docker"))
    by = {c: {o.action for o in m.operations if o.category == c} for c in m.categories}
    assert by["inspect"] >= {"ps", "logs", "inspect"}
    assert by["containers"] == {
        "container_create", "start", "stop", "restart", "pause", "unpause", "container_remove", "wait_ready"
    }
    assert by["images"] == {"image_pull", "image_tag", "image_remove", "build"}
    assert by["volumes"] == {"volume_create", "volume_remove"}
    assert by["networks"] == {"network_create", "network_connect", "network_disconnect", "network_remove"}
    assert by["compose"] == {
        "compose_config", "compose_ps", "compose_logs", "compose_up", "compose_stop", "compose_down"
    }


# ── Invalid manifests are rejected, never exposed ───────────────────────────


def _connector(rid="badapp", caps=None, **attrs):
    class Bad(ResourceConnector):
        resource_id = rid
        display_name = "Bad"

        async def connect(self):
            return ResourceResult.success()

        async def disconnect(self):
            return ResourceResult.success()

        def status(self):
            return ResourceStatus(rid, "Bad", ResourceState.DISCONNECTED)

        async def discover(self):
            return []

        def capabilities(self):
            if callable(caps):
                return caps()
            return caps if caps is not None else []

        async def execute(self, action, params=None):
            return ResourceResult.success(action)

    for k, v in attrs.items():
        setattr(Bad, k, v)
    return Bad()


READ = ResourcePermission.READ


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"rid": "Bad"}, "invalid integration id"),
        ({"rid": "has__double"}, "invalid integration id"),
        ({"rid": "-lead"}, "invalid integration id"),
        ({"rid": "x" * 30}, "invalid integration id"),
        ({"caps": []}, "no operations"),
        ({"caps": [ResourceCapability("Bad Name", READ)]}, "invalid operation name"),
        (
            {"caps": [ResourceCapability("a", READ), ResourceCapability("a", READ)]},
            "duplicate operation",
        ),
        ({"caps": [ResourceCapability("a", READ, scope="nodots")]}, "invalid permission scope"),
        ({"caps": [ResourceCapability("a", READ, scope="Upper.case")]}, "invalid permission scope"),
        ({"caps": [ResourceCapability("a", READ, category="Bad Cat")]}, "invalid category"),
        (
            {
                "caps": [
                    ResourceCapability("a", READ, params={"properties": {"x": {"type": "object"}}})
                ]
            },
            "unsupported type",
        ),
        (
            {
                "caps": [
                    ResourceCapability("a", READ, params={"properties": {"X": {"type": "string"}}})
                ]
            },
            "invalid parameter name",
        ),
        (
            {
                "caps": [
                    ResourceCapability("a", READ, params={"properties": {}, "required": ["ghost"]})
                ]
            },
            "not declared",
        ),
        ({"caps": lambda: (_ for _ in ()).throw(RuntimeError("boom"))}, "could not be listed"),
    ],
)
def test_invalid_manifests_are_rejected(kwargs, message):
    with pytest.raises(ManifestError, match=message):
        build_manifest(_connector(**kwargs))


def test_too_many_parameters_are_rejected():
    props = {f"p{i}": {"type": "string"} for i in range(17)}
    with pytest.raises(ManifestError, match="too many parameters"):
        model_schema({"properties": props})


def test_invalid_enum_is_rejected():
    with pytest.raises(ManifestError, match="enum"):
        model_schema({"properties": {"x": {"type": "integer", "enum": [1]}}})


async def test_catalog_reports_and_excludes_invalid_integrations(tmp_path):
    env = make_env(
        tmp_path,
        connectors={
            "fakeapp": FakeApp,
            "badapp": lambda: _connector("badapp", caps=[]),
        },
    )
    catalog = env.source.catalog
    assert [m.id for m in catalog.manifests()] == ["fakeapp"]
    assert "no operations" in catalog.problems()["badapp"]
    rows = {r["id"]: r for r in await catalog.describe()}
    assert rows["badapp"]["state"] == "invalid" and rows["fakeapp"]["state"] != "invalid"
    # An invalid integration cannot be enabled, and nothing about it leaks tools.
    result = await env.source.enable("badapp")
    assert result["enabled"] is False and result["error"]["code"] == "not_found"
    assert env.source.scope.enabled_ids() == []


def test_duplicate_registration_is_an_error_only_when_asked():
    manager = ResourceManager()
    manager.register("fakeapp", FakeApp)
    manager.register("fakeapp", OtherApp)  # replacement is the long-standing default
    with pytest.raises(ValueError, match="already registered"):
        manager.register("fakeapp", FakeApp, replace=False)


def test_integration_ids_cannot_collide_on_tool_names():
    """`<id>__<action>` is unambiguous because an id never contains '__'."""
    for rid in ("a__b", "a_", "_a", "a--"):
        with pytest.raises(ManifestError):
            build_manifest(_connector(rid, caps=[ResourceCapability("x", READ)]))


# ── Model-visible schemas are a safe, portable subset ───────────────────────


def test_schema_whitelists_keys_and_hides_limits():
    schema, constraints = model_schema(
        {
            "properties": {
                "n": {
                    "type": "integer",
                    "description": "How many",
                    "minimum": 1,
                    "maximum": 5,
                    "default": 3,
                    "format": "int32",
                    "x-evil": "ignored",
                },
                "mode": {"type": "string", "enum": ["a", "b"]},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["n"],
        }
    )
    assert schema == {
        "type": "object",
        "properties": {
            "n": {"type": "integer", "description": "How many"},
            "mode": {"type": "string", "enum": ["a", "b"]},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["n"],
    }
    assert constraints == {"n": {"minimum": 1, "maximum": 5}}


def test_descriptions_are_bounded_and_redacted():
    schema, _ = model_schema(
        {
            "properties": {
                "k": {"type": "string", "description": "use sk-ant-" + "a" * 30 + " " + "x" * 600}
            }
        }
    )
    text = schema["properties"]["k"]["description"]
    assert "sk-ant-" not in text and len(text) <= 300


def test_no_credentials_in_any_manifest_or_schema(tmp_path):
    """Configured secrets never enter descriptions or schemas shown to the model."""
    from velune.core.redaction import register_secret_value

    register_secret_value("pg-secret-pass-123")
    manager = ResourceManager()
    manager.register_defaults()
    from velune.resources.postgres.connector import PostgresConnector

    manager.register(
        "postgres",
        lambda: PostgresConnector(config={"host": "localhost", "password": "pg-secret-pass-123"}),
    )
    catalog = CapabilityCatalog(manager)
    dump = json.dumps(
        [
            {"d": m.description, "ops": [(o.description, o.schema) for o in m.operations]}
            for m in catalog.manifests()
        ]
    )
    assert "pg-secret-pass-123" not in dump and "localhost" not in dump


# ── Argument validation ─────────────────────────────────────────────────────

SCHEMA, LIMITS = model_schema(
    {
        "properties": {
            "name": {"type": "string"},
            "n": {"type": "integer", "minimum": 1, "maximum": 10},
            "on": {"type": "boolean"},
            "mode": {"type": "string", "enum": ["a", "b"]},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["name"],
    }
)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ("not a dict", "JSON object"),
        ({}, "missing required argument 'name'"),
        ({"name": "x", "extra": 1}, "unknown argument"),
        ({"name": 5}, "must be of type string"),
        ({"name": "x", "n": True}, "must be of type integer"),
        ({"name": "x", "n": 1.5}, "must be of type integer"),
        ({"name": "x", "n": None}, "must be of type integer"),
        ({"name": "x", "n": 0}, "below the minimum"),
        ({"name": "x", "n": 11}, "above the maximum"),
        ({"name": "x", "on": "yes"}, "must be of type boolean"),
        ({"name": "x", "mode": "c"}, "must be one of"),
        ({"name": "x", "tags": "a"}, "must be of type array"),
        ({"name": "x" * 20_001}, "too long"),
        ({"name": "x", "tags": ["a"] * 65}, "too many items"),
    ],
)
def test_arguments_are_validated_strictly(args, message):
    with pytest.raises(ValueError, match=message):
        validate_arguments(SCHEMA, LIMITS, args)


def test_valid_arguments_pass():
    validate_arguments(SCHEMA, LIMITS, {"name": "x", "n": 3, "on": False, "mode": "a", "tags": []})


def test_validation_messages_never_echo_values():
    secret = "s3cr3t-value-should-not-appear"
    wrong_type = {"name": "x", "n": secret}  # a string where an integer belongs
    too_long = {"name": secret * 2000}
    bad_enum = {"name": "x", "mode": secret}
    for bad in (wrong_type, too_long, bad_enum):
        with pytest.raises(ValueError) as info:
            validate_arguments(SCHEMA, LIMITS, bad)
        assert secret not in str(info.value)


# ── Result compaction ───────────────────────────────────────────────────────


def test_small_results_are_untouched():
    payload = {"ok": True, "data": {"a": 1}}
    assert compact_payload(payload) is payload


def test_large_list_results_keep_leading_items_and_the_true_total():
    payload = {"ok": True, "data": [{"row": i, "pad": "x" * 100} for i in range(500)]}
    out = compact_payload(payload, limit=3000)
    assert len(json.dumps(out)) <= 3000 + 500
    assert out["data"]["truncated"] and out["data"]["total_items"] == 500
    assert out["data"]["items"][0]["row"] == 0 and out["warnings"]


def test_large_log_results_keep_the_latest_lines():
    payload = {"ok": True, "data": {"job_id": "job-1", "lines": [f"line {i}" for i in range(2000)]}}
    out = compact_payload(payload, limit=2000)
    assert out["data"]["lines"][-1] == "line 1999" and out["data"]["truncated"]
    assert out["data"]["total_lines"] == 2000


def test_large_scalar_results_become_a_bounded_preview():
    out = compact_payload({"ok": True, "data": "y" * 50_000}, limit=2000)
    assert out["data"]["truncated"] and len(out["data"]["preview"]) < 2000
    json.dumps(out)  # still valid JSON


# ── Availability ────────────────────────────────────────────────────────────


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _catalog(tmp_path, app, clock=None, **kw):
    env = make_env(tmp_path, connectors={"fakeapp": lambda: app})
    return CapabilityCatalog(env.manager, clock=clock or Clock(), **kw), app


async def test_available_unavailable_and_unconfigured_are_distinguished(tmp_path):
    catalog, _ = _catalog(tmp_path, FakeApp())
    assert (await catalog.availability("fakeapp")).state == "available"
    catalog, _ = _catalog(tmp_path, FakeApp(available=False))
    a = await catalog.availability("fakeapp")
    assert a.state == "unavailable" and "not running" in a.reason and not a.usable
    catalog, _ = _catalog(tmp_path, FakeApp(problem="Not configured — run /resource configure"))
    a = await catalog.availability("fakeapp")
    assert a.state == "needs_configuration" and "configure" in a.reason


async def test_connected_integrations_report_live_state_without_probing(tmp_path):
    app = FakeApp()
    catalog, _ = _catalog(tmp_path, app)
    await app.connect()
    assert (await catalog.availability("fakeapp")).state == "connected"
    assert app.probes == 0
    await app.disconnect()
    assert (await catalog.availability("fakeapp")).state == "available"


async def test_unknown_integration_is_unknown(tmp_path):
    catalog, _ = _catalog(tmp_path, FakeApp())
    assert (await catalog.availability("ghost")).state == "unknown"


async def test_probe_results_are_cached_for_the_ttl_then_refreshed(tmp_path):
    clock = Clock()
    catalog, app = _catalog(tmp_path, FakeApp(), clock, availability_ttl=30)
    await catalog.availability("fakeapp")
    await catalog.availability("fakeapp")
    assert app.probes == 1
    clock.now += 31
    await catalog.availability("fakeapp")
    assert app.probes == 2
    await catalog.availability("fakeapp", refresh=True)
    assert app.probes == 3


async def test_stale_state_is_not_trusted_after_the_ttl(tmp_path):
    clock = Clock()
    catalog, app = _catalog(tmp_path, FakeApp(), clock, availability_ttl=30)
    assert (await catalog.availability("fakeapp")).usable
    app.available = False
    assert (await catalog.availability("fakeapp")).usable  # still within the TTL
    clock.now += 31
    assert not (await catalog.availability("fakeapp")).usable


async def test_note_failure_marks_unavailable_immediately(tmp_path):
    catalog, _ = _catalog(tmp_path, FakeApp())
    assert (await catalog.availability("fakeapp")).usable
    catalog.note_failure("fakeapp", "connection refused")
    a = await catalog.availability("fakeapp")
    assert a.state == "unavailable" and "refused" in a.reason


async def test_a_hanging_probe_times_out(tmp_path):
    class Hangs(FakeApp):
        async def probe_availability(self):
            await asyncio.sleep(30)
            return True, ""

    catalog, _ = _catalog(tmp_path, Hangs(), probe_timeout=0.05)
    a = await catalog.availability("fakeapp")
    assert a.state == "unavailable" and "in time" in a.reason


async def test_a_crashing_probe_is_unavailable_not_an_exception(tmp_path):
    class Crashes(FakeApp):
        async def probe_availability(self):
            raise RuntimeError("password=hunter2-secret-value")

    catalog, _ = _catalog(tmp_path, Crashes())
    a = await catalog.availability("fakeapp")
    assert a.state == "unavailable" and "hunter2" not in a.reason


async def test_a_crashing_configuration_check_is_reported_safely(tmp_path):
    class Crashes(FakeApp):
        def configuration_problem(self):
            raise RuntimeError("boom")

    catalog, _ = _catalog(tmp_path, Crashes())
    assert (await catalog.availability("fakeapp")).state == "needs_configuration"


async def test_docker_probe_needs_a_running_daemon_not_just_the_cli():
    from tests.docker_fakes import FakeDaemon, make_engine
    from velune.resources.docker.connector import DockerConnector

    daemon = FakeDaemon()
    connector = DockerConnector(engine=make_engine(daemon), workspace_trusted=True)
    assert await connector.probe_availability() == (True, "")
    daemon.down = True
    connector = DockerConnector(engine=make_engine(daemon), workspace_trusted=True)
    ok, reason = await connector.probe_availability()
    assert not ok and "not running or not reachable" in reason


async def test_sql_connectors_need_stored_configuration(monkeypatch):
    from velune.resources import sql_base
    from velune.resources.postgres.connector import PostgresConnector

    monkeypatch.setattr(sql_base, "load_resource_secret", lambda *a: None)
    problem = PostgresConnector().configuration_problem()
    assert "/resource configure postgres" in problem
    assert PostgresConnector(config={"host": "localhost"}).configuration_problem() is None


# ── Selection is deterministic ──────────────────────────────────────────────


def _suggest_env(tmp_path):
    return make_env(
        tmp_path,
        connectors={"fakeapp": FakeApp, "otherapp": OtherApp, "bigapp": many_ops_app(2)},
    ).source.catalog


def test_suggestions_match_whole_keywords_only(tmp_path):
    c = _suggest_env(tmp_path)
    assert c.suggest("please read the widget") == ["fakeapp"]
    assert c.suggest("a widgetize thing") == []  # no substring matches
    assert c.suggest("") == [] and c.suggest("nothing relevant here") == []


def test_suggestions_rank_by_score_then_registration_order(tmp_path):
    c = _suggest_env(tmp_path)
    assert c.suggest("fake widget and other") == ["fakeapp", "otherapp"]
    assert c.suggest("other big") == ["otherapp", "bigapp"]  # tie keeps registration order


def test_the_integration_id_and_workspace_hints_count(tmp_path):
    c = _suggest_env(tmp_path)
    assert c.suggest("use fakeapp") == ["fakeapp"]
    hint = DiscoveryHint("bigapp", "Big", source="x")
    assert c.suggest("nothing", [hint]) == ["bigapp"]


def test_suggestion_is_stable_across_calls(tmp_path):
    c = _suggest_env(tmp_path)
    assert [c.suggest("fake other big") for _ in range(5)] == [c.suggest("fake other big")] * 5


async def test_describe_rows_and_query_filter(tmp_path):
    env = make_env(tmp_path, connectors={"fakeapp": FakeApp, "otherapp": OtherApp})
    rows = await env.source.catalog.describe(task_text="read a widget")
    by = {r["id"]: r for r in rows}
    assert by["fakeapp"]["suggested"] and not by["otherapp"]["suggested"]
    assert by["fakeapp"]["categories"][0] == {"name": "inspect", "operations": ["peek", "count"]}
    assert by["fakeapp"]["operations"] == 4 and by["fakeapp"]["state"] == "available"
    only = await env.source.catalog.describe(query="second")
    assert [r["id"] for r in only] == ["otherapp"]


async def test_disabled_integrations_are_invisible(tmp_path):
    env = make_env(tmp_path)
    env.manager._config = {"fakeapp": {"enabled": False}}
    assert await env.source.catalog.describe() == []
    assert (await env.source.enable("fakeapp"))["error"]["code"] == "not_found"


# ── Scope and budget ────────────────────────────────────────────────────────


def _manifest(app_cls=FakeApp):
    return build_manifest(app_cls())


def test_enable_is_idempotent_and_versioned():
    scope = CapabilityScope()
    m = _manifest()
    assert scope.version == 0
    first = scope.enable(m)
    assert first.changed and scope.version == 1 and len(first.exposed) == 4
    again = scope.enable(m)
    assert not again.changed and scope.version == 1
    assert [o.tool_name for o in scope.exposed] == [o.tool_name for o in first.exposed]


def test_categories_narrow_and_merge():
    scope = CapabilityScope()
    m = _manifest()
    scope.enable(m, ["inspect"])
    assert {o.action for o in scope.exposed} == {"peek", "count"}
    scope.enable(m, ["change"])
    assert {o.action for o in scope.exposed} == {"peek", "count", "poke", "nuke"}


def test_unknown_categories_are_reported_and_enable_nothing_alone():
    scope = CapabilityScope()
    out = scope.enable(_manifest(), ["nope"])
    assert out.unknown_categories == ("nope",) and not out.exposed and scope.enabled_ids() == []
    out = scope.enable(_manifest(), ["inspect", "nope"])
    assert out.unknown_categories == ("nope",) and len(out.exposed) == 2


def test_disable_and_reset():
    scope = CapabilityScope()
    scope.enable(_manifest())
    assert scope.disable("fakeapp") and not scope.disable("fakeapp") and scope.exposed == ()
    scope.enable(_manifest())
    scope.reset()
    assert scope.enabled_ids() == [] and scope.dispatched == ()


def test_only_enabled_operations_resolve():
    scope = CapabilityScope()
    scope.enable(_manifest(), ["inspect"])
    assert scope.is_enabled("fakeapp", "peek")
    assert not scope.is_enabled("fakeapp", "poke")  # a different category
    assert not scope.is_enabled("fakeapp", "ghost") and not scope.is_enabled("ghost", "peek")


def test_overflow_goes_to_the_dispatcher_instead_of_being_dropped():
    scope = CapabilityScope(max_tools=4)
    scope.enable(build_manifest(many_ops_app(10)()))
    assert len(scope.exposed) == 3 and len(scope.dispatched) == 7  # one slot is the dispatcher
    assert len(scope.exposed) + 1 <= scope.max_tools
    everything = {o.action for o in (*scope.exposed, *scope.dispatched)}
    assert everything == {f"op{i:02d}" for i in range(10)}  # nothing lost
    assert all(scope.is_enabled("bigapp", a) for a in everything)


def test_no_dispatcher_slot_is_reserved_when_everything_fits():
    scope = CapabilityScope(max_tools=4)
    scope.enable(build_manifest(many_ops_app(4)()))
    assert len(scope.exposed) == 4 and scope.dispatched == ()


def test_tool_order_is_the_order_of_enabling():
    scope = CapabilityScope()
    scope.enable(build_manifest(OtherApp()))
    scope.enable(_manifest())
    assert [o.integration for o in scope.exposed][:1] == ["otherapp"]
    assert [o.integration for o in scope.exposed][-1] == "fakeapp"
    before = [o.tool_name for o in scope.exposed]
    scope.enable(build_manifest(many_ops_app(2)()))
    assert [o.tool_name for o in scope.exposed][: len(before)] == before  # append-only


def test_a_minimum_budget_is_enforced():
    assert CapabilityScope(max_tools=0).max_tools == 4


async def test_refresh_drops_a_removed_integration_and_tracks_changes(tmp_path):
    env = make_env(tmp_path)
    await env.source.enable("fakeapp")
    assert env.source.scope.enabled_ids() == ["fakeapp"]
    version = env.source.scope.version
    assert not env.source.scope.refresh(env.source.catalog)  # nothing changed
    assert env.source.scope.version == version
    env.manager._config = {"fakeapp": {"enabled": False}}  # removed since last turn
    assert env.source.scope.refresh(env.source.catalog)
    assert env.source.scope.enabled_ids() == [] and env.source.scope.exposed == ()


async def test_refresh_picks_up_a_redefined_connector(tmp_path):
    env = make_env(tmp_path)
    await env.source.enable("fakeapp")

    class Redefined(FakeApp):
        def capabilities(self):
            return [c for c in super().capabilities() if c.action != "nuke"]

    env.manager._connectors["fakeapp"] = Redefined()
    assert env.source.scope.refresh(env.source.catalog)
    assert {o.action for o in env.source.scope.exposed} == {"peek", "count", "poke"}
