"""A plugin's hooks and MCP servers do not inherit Velune's environment (F-07).

Hooks and stdio MCP servers inherited the whole parent environment, where provider API keys,
cloud credentials and tokens normally live, so any plugin could read them. The MCP SDK ships a
safe default for this and Velune passed the full environment over it. A plugin's children now
get a small allowlist plus what the plugin itself declares; the user's own hooks and servers are
unchanged. The only subprocess started here is a probe script written by this test.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

from velune.core.child_env import plugin_child_env
from velune.hooks.config import load_hooks
from velune.hooks.dispatcher import HookDispatcher
from velune.hooks.executor import run_hook
from velune.hooks.types import HookDefinition
from velune.mcp.registry import MCPServerRegistry
from velune.mcp.transports.base import ServerConfig
from velune.mcp.transports.stdio import StdioConnection
from velune.plugins.manager import PluginManager

# Names a real environment holds secrets under, plus ones no pattern would catch.
SECRETS = {
    "OPENAI_API_KEY": "sk-openai-SECRET",
    "ANTHROPIC_API_KEY": "sk-ant-SECRET",
    "GOOGLE_API_KEY": "google-SECRET",
    "AWS_ACCESS_KEY_ID": "AKIA-SECRET",
    "AWS_SECRET_ACCESS_KEY": "aws-secret-SECRET",
    "AWS_SESSION_TOKEN": "aws-session-SECRET",
    "AZURE_CLIENT_SECRET": "azure-SECRET",
    "GITHUB_TOKEN": "ghp_SECRET",
    "GH_TOKEN": "gh-SECRET",
    "HF_TOKEN": "hf_SECRET",
    "NPM_TOKEN": "npm-SECRET",
    "DATABASE_URL": "postgres://user:db-SECRET@host/db",
    "VELUNE_MASTER_PASSPHRASE": "passphrase-SECRET",
    "SSH_AUTH_SOCK": "/tmp/ssh-SECRET/agent",
    "MY_INTERNAL_THING": "unexpected-name-SECRET",  # no pattern would have caught this one
}

SYSTEM = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/u",
    "USER": "u",
    "LOGNAME": "u",
    "SHELL": "/bin/sh",
    "TERM": "xterm",
    "LANG": "en_US.UTF-8",
    "LC_ALL": "C",
    "LC_CTYPE": "C",
    "TMPDIR": "/tmp",
    "TZ": "UTC",
}


@pytest.fixture
def secret_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    return SECRETS


# ── the policy ────────────────────────────────────────────────────────────────────────────────


def test_no_secret_reaches_a_plugin_child_whatever_it_is_called():
    env = plugin_child_env(base={**SYSTEM, **SECRETS}, windows=False)
    assert not set(env) & set(SECRETS)
    assert not any("SECRET" in value for value in env.values())


def test_the_system_variables_a_program_needs_are_kept_on_posix():
    env = plugin_child_env(base={**SYSTEM, **SECRETS}, windows=False)
    assert env == SYSTEM  # exactly the allowlist, nothing else


def test_windows_keeps_the_system_folders_and_matches_names_case_insensitively():
    base = {
        "Path": r"C:\Windows\system32",
        "SystemRoot": r"C:\Windows",
        "ComSpec": r"C:\Windows\system32\cmd.exe",
        "USERPROFILE": r"C:\Users\u",
        "TEMP": r"C:\Temp",
        "OPENAI_API_KEY": "sk-SECRET",
        "HOME": "not-a-windows-variable",
        "LANG": "not-a-windows-variable",
    }
    env = plugin_child_env(base=base, windows=True)
    assert env == {
        "Path": r"C:\Windows\system32",
        "SystemRoot": r"C:\Windows",
        "ComSpec": r"C:\Windows\system32\cmd.exe",
        "USERPROFILE": r"C:\Users\u",
        "TEMP": r"C:\Temp",
    }


def test_posix_names_are_case_sensitive():
    env = plugin_child_env(base={"path": "/evil", "PATH": "/bin"}, windows=False)
    assert env == {"PATH": "/bin"}


def test_exported_shell_functions_are_dropped():
    env = plugin_child_env(base={"PATH": "() { evil; }", "HOME": "/h"}, windows=False)
    assert env == {"HOME": "/h"}


def test_what_the_plugin_declares_is_passed_and_wins():
    env = plugin_child_env(
        {"PATH": "/plugin/bin", "PLUGIN_TOKEN": "declared-by-the-plugin"},
        base=SYSTEM,
        windows=False,
    )
    assert env["PATH"] == "/plugin/bin"
    assert env["PLUGIN_TOKEN"] == "declared-by-the-plugin"


def test_a_declared_reference_to_a_parent_variable_is_not_expanded():
    env = plugin_child_env({"KEY": "${OPENAI_API_KEY}"}, base={**SYSTEM, **SECRETS}, windows=False)
    assert env["KEY"] == "${OPENAI_API_KEY}"  # there is deliberately no forwarding syntax


def test_the_workspace_is_exposed_only_when_given():
    assert "VELUNE_WORKSPACE" not in plugin_child_env(base=SYSTEM, windows=False)
    env = plugin_child_env(base=SYSTEM, windows=False, workspace=Path("/repo"))
    assert env["VELUNE_WORKSPACE"] == str(Path("/repo"))


def test_the_policy_does_not_mutate_what_it_is_given():
    base, declared = dict(SYSTEM), {"A": "1"}
    plugin_child_env(declared, base=base, windows=False)
    assert base == SYSTEM and declared == {"A": "1"}


def test_the_default_source_is_the_real_environment(secret_env, monkeypatch):
    monkeypatch.setenv("PATH", "/some/bin")
    env = plugin_child_env()
    assert "PATH" in env
    assert not set(env) & set(secret_env)


# ── entries the operating system would refuse ──────────────────────────────────────────────────

INVALID_DECLARED = [
    ("A=B", "value"),  # a name containing "="
    ("", "value"),  # an empty name
    ("NUL_IN_NAME\x00x", "value"),
    ("NUL_IN_VALUE", "a\x00b"),
    ("NOT_TEXT", 5),
    ("LIST", ["a"]),
    (7, "value"),
    ("NONE_VALUE", None),
]


@pytest.mark.parametrize(("key", "value"), INVALID_DECLARED)
def test_an_invalid_declared_entry_is_dropped_and_the_rest_kept(key, value, caplog):
    declared = {"GOOD": "yes", key: value}
    with caplog.at_level(logging.WARNING, logger="velune.core.child_env"):
        env = plugin_child_env(declared, base=SYSTEM, windows=False)
    assert env["GOOD"] == "yes"
    assert key not in env
    assert "Ignoring an invalid environment entry" in caplog.text


def test_a_dropped_entrys_value_is_never_logged(caplog):
    with caplog.at_level(logging.DEBUG):
        plugin_child_env({"BAD=NAME": "super-SECRET-value"}, base=SYSTEM, windows=False)
    assert "super-SECRET-value" not in caplog.text


@pytest.mark.parametrize("scrub_env", [True, False])
@pytest.mark.parametrize(
    "env", [{"A=B": "v"}, {"X": "a\x00SECRET"}, {"": "v"}, {"X": 5}, {"X": ["a"]}]
)
async def test_a_hook_that_cannot_start_is_a_no_op_not_an_error(tmp_path, caplog, scrub_env, env):
    """The OS refuses some environments (a NUL, a name with "="). That used to raise out of
    run_hook into the tool call that fired it."""
    definition = HookDefinition(
        type="command",
        command=f'"{sys.executable}" -c "print(1)"',
        timeout=20,
        env=env,
        scrub_env=scrub_env,
    )
    with caplog.at_level(logging.DEBUG):
        result = await run_hook(definition, {}, workspace=tmp_path)  # must not raise
    assert result.blocked is False
    assert "SECRET" not in caplog.text


# ── hooks: a real child process ───────────────────────────────────────────────────────────────


@pytest.fixture
def probe(tmp_path: Path) -> str:
    script = tmp_path / "probe.py"
    script.write_text(
        "import json, os\nprint(json.dumps({'systemMessage': json.dumps(dict(os.environ))}))\n"
    )
    return f'"{sys.executable}" "{script}"'


async def child_env_of(definition: HookDefinition, workspace: Path) -> dict[str, str]:
    result = await run_hook(definition, {"hook_event_name": "PreToolUse"}, workspace=workspace)
    assert result.system_message, "the probe did not run"
    return json.loads(result.system_message)


async def test_a_plugin_hook_child_does_not_see_the_secrets(secret_env, probe, tmp_path):
    definition = HookDefinition(
        type="command", command=probe, timeout=30, env={"DECLARED": "yes"}, scrub_env=True
    )
    env = await child_env_of(definition, tmp_path)

    assert not set(env) & set(secret_env)
    assert not any("SECRET" in value for value in env.values())
    assert env["DECLARED"] == "yes"  # what the plugin declared arrives
    assert env["VELUNE_WORKSPACE"] == str(tmp_path)
    assert env.get("PATH")  # and the child could start at all


async def test_the_users_own_hook_still_inherits_the_environment(secret_env, probe, tmp_path):
    definition = HookDefinition(type="command", command=probe, timeout=30)  # scrub_env False
    env = await child_env_of(definition, tmp_path)
    assert env["OPENAI_API_KEY"] == secret_env["OPENAI_API_KEY"]  # unchanged behaviour


async def test_env_overrides_still_apply_to_a_plugin_hook(secret_env, probe, tmp_path):
    definition = HookDefinition(type="command", command=probe, timeout=30, scrub_env=True)
    result = await run_hook(
        definition, {}, workspace=tmp_path, env_overrides={"FROM_VELUNE": "payload-id"}
    )
    assert json.loads(result.system_message)["FROM_VELUNE"] == "payload-id"


# ── MCP: what the server process is started with ──────────────────────────────────────────────


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Capture the environment a stdio server would be started with; start nothing."""
    import mcp.client.stdio as mcp_stdio

    captured: dict = {}

    class _Refuses:
        async def __aenter__(self):
            raise FileNotFoundError("no such server (the test starts nothing)")

        async def __aexit__(self, *exc):
            return None

    def fake_stdio_client(params):
        captured["env"] = dict(params.env or {})
        return _Refuses()

    monkeypatch.setattr(mcp_stdio, "stdio_client", fake_stdio_client)
    return captured


async def spawn_env(config: ServerConfig) -> None:
    from velune.mcp.transports.base import MCPTransportError

    with pytest.raises(MCPTransportError):
        await StdioConnection(config).connect()


async def test_a_plugin_mcp_server_does_not_get_the_secrets(secret_env, spawned):
    config = ServerConfig.from_dict("p:s", {"command": "x", "env": {"DECLARED": "yes"}})
    config.scrub_env = True

    await spawn_env(config)

    assert not set(spawned["env"]) & set(secret_env)
    assert spawned["env"]["DECLARED"] == "yes"
    assert spawned["env"].get("PATH")


async def test_the_users_own_mcp_server_still_inherits_the_environment(secret_env, spawned):
    config = ServerConfig.from_dict("mine", {"command": "x", "env": {"DECLARED": "yes"}})
    assert config.scrub_env is False

    await spawn_env(config)

    assert spawned["env"]["OPENAI_API_KEY"] == secret_env["OPENAI_API_KEY"]
    assert spawned["env"]["DECLARED"] == "yes"


# ── what the wiring marks, and what a config file cannot change ───────────────────────────────


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: path))
    return path


def make_plugin(plugins_dir: Path, hooks: dict, servers: dict) -> None:
    root = plugins_dir / "p"
    (root / ".velune-plugin").mkdir(parents=True)
    (root / ".velune-plugin" / "plugin.json").write_text(json.dumps({"name": "p"}))
    (root / "hooks").mkdir()
    (root / "hooks" / "hooks.json").write_text(json.dumps(hooks))
    (root / ".mcp.json").write_text(json.dumps(servers))


def test_every_plugin_hook_and_server_is_marked_even_if_its_config_says_otherwise(tmp_path, home):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    make_plugin(
        workspace / ".velune" / "plugins",
        hooks={
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": "echo x", "scrub_env": False}],
                }
            ]
        },
        servers={"s": {"command": "echo", "scrub_env": False}},  # tries to opt out
    )
    manager = PluginManager(workspace=workspace, workspace_trusted=True)
    manager.load()
    dispatcher = HookDispatcher(workspace=workspace, trusted=True)
    registry = MCPServerRegistry(workspace=workspace)

    manager.wire_hooks(dispatcher)
    manager.wire_mcp(registry)

    definitions = [d for b in dispatcher._ensure_loaded() for d in b.hooks]
    assert definitions and all(d.scrub_env for d in definitions)
    assert registry._entries["p:s"].config.scrub_env is True


def test_the_users_own_config_files_are_never_scrubbed(tmp_path, home):
    (home / ".velune").mkdir()
    (home / ".velune" / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "x"}]}]
                }
            }
        )
    )
    (home / ".mcp.json").write_text(json.dumps({"mine": {"command": "x"}}))
    workspace = tmp_path / "repo"
    workspace.mkdir()

    hooks = [d for b in load_hooks(workspace, trusted=False) for d in b.hooks]
    registry = MCPServerRegistry(workspace=workspace)
    registry.load_config(trusted=False)

    assert hooks and not any(d.scrub_env for d in hooks)
    assert registry._entries["mine"].config.scrub_env is False


def test_a_config_file_cannot_turn_scrubbing_on_or_off_for_the_users_own_server(tmp_path, home):
    (home / ".mcp.json").write_text(json.dumps({"mine": {"command": "x", "scrub_env": True}}))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    registry = MCPServerRegistry(workspace=workspace)
    registry.load_config(trusted=False)
    assert registry._entries["mine"].config.scrub_env is False  # the flag is not read from files


# ── nothing leaks through logs or errors ──────────────────────────────────────────────────────


async def test_secrets_do_not_appear_in_logs_or_errors_when_a_scrubbed_hook_fails(
    secret_env, tmp_path, caplog
):
    definition = HookDefinition(
        type="command",
        command=f'"{sys.executable}" -c "import sys; sys.exit(3)" no-such-arg',
        timeout=30,
        scrub_env=True,
    )
    missing = HookDefinition(
        type="command", command="definitely-not-a-real-program-xyz", timeout=10, scrub_env=True
    )
    with caplog.at_level(logging.DEBUG):
        await run_hook(definition, {}, workspace=tmp_path)
        await run_hook(missing, {}, workspace=tmp_path)

    for value in secret_env.values():
        assert value not in caplog.text


async def test_secrets_do_not_appear_in_the_error_when_a_scrubbed_server_fails(
    secret_env, spawned, caplog
):
    from velune.mcp.transports.base import MCPTransportError

    config = ServerConfig.from_dict("p:s", {"command": "x"})
    config.scrub_env = True
    with caplog.at_level(logging.DEBUG), pytest.raises(MCPTransportError) as excinfo:
        await StdioConnection(config).connect()

    text = caplog.text + str(excinfo.value)
    for value in secret_env.values():
        assert value not in text
