"""Configuration, manager wiring, availability and `/resource configure` (VAIR A5)."""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from rich.console import Console

from tests.sql_fakes import (
    SECRET_PASSWORD,
)
from velune.kernel.config import DatabaseConfig, ResourcesConfig
from velune.resources.manager import build_default_manager
from velune.resources.mysql.connector import MySQLConnector
from velune.resources.postgres.connector import PostgresConnector
from velune.resources.supabase.connector import SupabaseConnector

# ── Configuration model ─────────────────────────────────────────────────────


def test_defaults_are_safe_and_bounded():
    cfg = ResourcesConfig()
    for section in (cfg.postgres, cfg.mysql, cfg.supabase):
        assert isinstance(section, DatabaseConfig)
        assert section.connect_timeout == 5 and section.statement_timeout == 30
        assert section.max_rows == 200 and section.idle_transaction_timeout == 60
        assert section.connections == {} and section.default_connection == ""


@pytest.mark.parametrize(
    "bad",
    [
        {"statement_timeout": 0},
        {"statement_timeout": 10_000},
        {"max_rows": 0},
        {"max_rows": 10**9},
        {"connect_timeout": -1},
        {"idle_transaction_timeout": 0},
        {"connections": {"a": {"port": 70000}}},
        {"connections": {"a": {"max_rows": 0}}},
    ],
)
def test_invalid_database_settings_are_rejected(bad):
    with pytest.raises(ValidationError):
        DatabaseConfig.model_validate(bad)


def test_a_connection_entry_has_no_password_field():
    from velune.kernel.config import DatabaseConnectionConfig

    fields = set(DatabaseConnectionConfig.model_fields)
    assert "password" not in fields and "token" not in fields and "service_role_key" not in fields


def test_the_config_survives_a_dump_and_validate_round_trip():
    cfg = ResourcesConfig.model_validate(
        {"postgres": {"connections": {"dev": {"read_only": True, "environment": "dev"}}}}
    )
    again = ResourcesConfig.model_validate(cfg.model_dump())
    assert again.postgres.connections["dev"].read_only is True


# ── The manager builds each connector from its own settings ─────────────────


def test_the_manager_passes_each_integrations_settings_and_workspace(tmp_path):
    cfg = ResourcesConfig.model_validate(
        {
            "postgres": {"statement_timeout": 7, "default_connection": "dev"},
            "mysql": {"max_rows": 33},
            "supabase": {"max_rows": 11},
        }
    ).model_dump()
    manager = build_default_manager(config=cfg, workspace=tmp_path)
    pg, my, sb = (manager.get(n) for n in ("postgres", "mysql", "supabase"))
    assert isinstance(pg, PostgresConnector) and pg.settings.statement_timeout == 7
    assert isinstance(my, MySQLConnector) and my.settings.max_rows == 33
    assert isinstance(sb, SupabaseConnector) and sb.settings.max_rows == 11
    assert pg.workspace == tmp_path


def test_a_disabled_database_integration_is_hidden(tmp_path):
    cfg = ResourcesConfig.model_validate({"mysql": {"enabled": False}}).model_dump()
    manager = build_default_manager(config=cfg, workspace=tmp_path)
    assert manager.get("mysql") is None and "mysql" not in manager.list_ids()
    assert manager.get("postgres") is not None


def test_a_bare_manager_still_builds_the_connectors_with_defaults():
    manager = build_default_manager()
    assert manager.get("postgres").settings.max_rows == 200


# ── Availability reflects configuration and reachability ────────────────────


class _Writer:
    def close(self):
        pass


def _socket(monkeypatch, ok=True):
    async def fake_open(host, port):
        if not ok:
            raise OSError("refused")
        return object(), _Writer()

    monkeypatch.setattr(asyncio, "open_connection", fake_open)


async def test_unconfigured_means_not_available(sql_env):
    sql_env.add("dev")
    c = sql_env.pg()
    c.real_probe = True
    sql_env.store.clear()
    ok, reason = await c.probe_availability()
    assert not ok and "not configured" in reason
    assert "/resource configure postgres" in c.configuration_problem()


async def test_a_reachable_server_is_available_an_unreachable_one_is_not(sql_env, monkeypatch):
    sql_env.add("dev")
    c = sql_env.pg()
    c.real_probe = True
    _socket(monkeypatch, ok=True)
    assert await c.probe_availability() == (True, "")
    _socket(monkeypatch, ok=False)
    ok, reason = await c.probe_availability()
    assert not ok and "not reachable" in reason


async def test_a_unix_socket_host_is_not_port_probed(sql_env, monkeypatch):
    sql_env.add("sock", host="/var/run/postgresql")
    c = sql_env.pg()
    c.real_probe = True
    _socket(monkeypatch, ok=False)  # would fail if a TCP probe were attempted
    assert await c.probe_availability() == (True, "")


async def test_several_connections_and_no_default_still_counts_as_available(sql_env, monkeypatch):
    sql_env.add("a")
    sql_env.add("b")
    c = sql_env.pg()
    c.real_probe = True
    assert await c.probe_availability() == (True, "")


async def test_a_missing_driver_is_not_available(sql_env):
    sql_env.add("dev")
    c = sql_env.pg()
    c.real_probe = True
    c.driver_available = lambda: False  # type: ignore[method-assign]
    ok, reason = await c.probe_availability()
    assert not ok and "driver" in reason


async def test_one_unreachable_database_does_not_mark_the_others_unavailable(sql_env, tmp_path):
    import json

    from tests.integration_fakes import ScriptedProvider, call, final, make_env, turn

    sql_env.add("dev")
    down = sql_env.pg()
    down.real_probe = False

    async def not_reachable():
        return False, "the database server is not reachable"

    down.probe_availability = not_reachable  # type: ignore[method-assign]
    other = sql_env.mysql()
    sql_env.add("m", engine="mysql")
    env = make_env(tmp_path, connectors={"postgres": lambda: down, "mysql": lambda: other})
    provider = ScriptedProvider(turn(call("integrations_list", "l1")), final("ok"))
    result = await env.run(provider, "what is available?")
    rows = {r["id"]: r for r in json.loads(result.invocations[0].result)["integrations"]}
    assert rows["postgres"]["state"] == "unavailable"
    assert rows["mysql"]["state"] != "unavailable"


# ── /resource configure: named connections and options (no extra prompts) ───


class _Answers:
    """Scripted replies for rich's Prompt (the interactive questions are unchanged)."""

    def __init__(self, monkeypatch, texts):
        from rich import prompt

        self.texts = list(texts)
        self.asked: list[str] = []
        monkeypatch.setattr(
            prompt.Prompt, "ask", classmethod(lambda cls, q, **kw: self._text(q, kw))
        )

        def never(cls, *a, **k):
            raise AssertionError("configure must not ask any further question")

        monkeypatch.setattr(prompt.Confirm, "ask", classmethod(never))

    def _text(self, question, kw):
        self.asked.append(question)
        return self.texts.pop(0) if self.texts else kw.get("default", "")


def _repl():
    manager = SimpleNamespace(discover=lambda: asyncio.sleep(0, result=[]))
    return SimpleNamespace(
        console=Console(file=io.StringIO(), width=140), _resource_manager=manager
    )


SQL_ANSWERS = ["localhost", "5432", "app", "dev", SECRET_PASSWORD]


@pytest.fixture
def saved(monkeypatch):
    out: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        "velune.resources.secrets.save_resource_secret", lambda t, n, c: out.append((t, n, dict(c)))
    )
    return out


async def test_configure_asks_exactly_the_same_five_questions_as_before(monkeypatch, saved):
    from velune.cli.handlers.resources import _configure

    answers = _Answers(monkeypatch, SQL_ANSWERS)
    await _configure(_repl(), "postgres")
    assert len(answers.asked) == 5
    assert saved == [
        (
            "postgres",
            "postgres",
            {
                "host": "localhost",
                "port": 5432,
                "database": "app",
                "username": "dev",
                "password": SECRET_PASSWORD,
            },
        )
    ]


async def test_a_named_connection_takes_its_policy_from_options(monkeypatch, saved):
    from velune.cli.handlers.resources import _configure

    _Answers(monkeypatch, SQL_ANSWERS)
    repl = _repl()
    await _configure(repl, "postgres staging --env Staging --read-only")
    ((rtype, name, config),) = saved
    assert (rtype, name) == ("postgres", "staging")
    assert config["environment"] == "staging" and config["read_only"] is True
    assert "allow_remote" not in config
    out = repl.console.file.getvalue()
    assert SECRET_PASSWORD not in out and "connection=staging" in out


async def test_options_may_come_before_the_name(monkeypatch, saved):
    from velune.cli.handlers.resources import _configure

    _Answers(monkeypatch, SQL_ANSWERS)
    await _configure(_repl(), "mysql --read-only reporting")
    assert saved[0][:2] == ("mysql", "reporting") and saved[0][2]["read_only"] is True


@pytest.mark.parametrize(
    "argument",
    [
        "postgres Bad Name!",
        "postgres a b",
        "postgres staging --nope",
        "postgres staging --env",
        "postgres staging --env --read-only",
        "postgres staging --allow-remote --tls plain",
        "postgres UPPER",
    ],
)
async def test_bad_names_and_options_are_refused_before_anything_is_asked(
    monkeypatch, saved, argument
):
    from velune.cli.handlers.resources import _configure

    answers = _Answers(monkeypatch, SQL_ANSWERS)
    repl = _repl()
    await _configure(repl, argument)
    assert saved == [] and answers.asked == []
    assert repl.console.file.getvalue().strip()  # the user is told why


async def test_a_remote_host_is_refused_unless_the_options_allow_it_with_tls(monkeypatch, saved):
    from velune.cli.handlers.resources import _configure

    remote = ["db.example.com", "5432", "app", "dev", "pw"]
    _Answers(monkeypatch, remote)
    repl = _repl()
    await _configure(repl, "postgres far")
    assert "allow_remote" not in saved[0][2] and "sslmode" not in saved[0][2]
    assert "--allow-remote" in repl.console.file.getvalue()  # and it says how

    _Answers(monkeypatch, remote)
    await _configure(_repl(), "postgres far --allow-remote")
    assert saved[1][2]["allow_remote"] is True and saved[1][2]["sslmode"] == "verify-full"

    _Answers(monkeypatch, remote)
    await _configure(_repl(), "postgres far --allow-remote --tls require --env production")
    assert saved[2][2]["sslmode"] == "require" and saved[2][2]["environment"] == "production"


async def test_allowing_remote_has_no_effect_on_a_local_host(monkeypatch, saved):
    from velune.cli.handlers.resources import _configure

    _Answers(monkeypatch, SQL_ANSWERS)
    await _configure(_repl(), "postgres dev --allow-remote")
    assert "allow_remote" not in saved[0][2]


async def test_supabase_takes_an_environment_but_ignores_sql_options(monkeypatch, saved):
    from velune.cli.handlers.resources import _configure

    _Answers(monkeypatch, ["https://abc.supabase.co", "anon", ""])
    await _configure(_repl(), "supabase stg --env staging --read-only")
    assert saved[0][:2] == ("supabase", "stg")
    assert saved[0][2] == {
        "url": "https://abc.supabase.co",
        "anon_key": "anon",
        "environment": "staging",
    }
