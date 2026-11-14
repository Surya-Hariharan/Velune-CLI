"""Built-in slash commands cannot be shadowed by a plugin or a command file (F-10c).

A plugin command used to replace any built-in of the same name or alias (a log warning was the
only protection), and the command-file loader only compared *names*, so ``q`` or ``h`` (aliases
of built-ins) could be taken over. The registry now seals the built-ins and refuses a protected,
reserved or already-held key, whatever its spelling. Everything here uses the real built-in
registry; plugin handlers are recorders, nothing is executed.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from velune.cli.commands import file_commands
from velune.cli.handlers.plugins import register_plugin_commands
from velune.cli.repl import VeluneREPL
from velune.cli.slash_commands import (
    RESERVED_COMMAND_NAMES,
    SlashCommand,
    SlashCommandRegistry,
    normalize_command_key,
)
from velune.cli.slash_dispatcher import _load_file_commands, build_slash_registry
from velune.plugins.declarative.command import PluginCommand


@pytest.fixture
def user_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "user-commands"
    path.mkdir()
    monkeypatch.setattr(file_commands, "_USER_COMMANDS_DIR", path)
    return path


def fake_repl(workspace: Path) -> MagicMock:
    repl = MagicMock()
    repl.container = SimpleNamespace(get=lambda key: str(workspace), get_optional=lambda k: None)
    repl._completer = None
    repl._command_palette = None
    return repl


@pytest.fixture
def repl(tmp_path: Path, user_commands) -> MagicMock:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    repl = fake_repl(workspace)
    repl._registry = build_slash_registry(repl)
    return repl


def plugin_command(name: str, aliases: tuple[str, ...] = ()) -> PluginCommand:
    return PluginCommand(
        name=name, plugin_name="p", aliases=list(aliases), body="BODY", usage=f"/{name}"
    )


def plugin(name: str, *commands: PluginCommand) -> SimpleNamespace:
    return SimpleNamespace(root=Path("."), name=name, commands=list(commands))


def command(name: str, *, aliases=(), owner: str = "", handler=None) -> SlashCommand:
    return SlashCommand(
        name=name,
        aliases=list(aliases),
        description="d",
        usage=f"/{name}",
        handler=handler or AsyncMock(),
        owner=owner,
    )


# ── every built-in is protected ───────────────────────────────────────────────────────────────


def test_every_builtin_name_and_alias_is_refused_for_a_plugin_command(repl):
    registry = repl._registry
    before = dict(registry._commands)
    keys = sorted(before)
    assert len(keys) > 50  # the real registry: all the built-ins and their aliases

    taken_over = []
    for key in keys:
        register_plugin_commands(repl, [plugin("evil", plugin_command(key))])
        register_plugin_commands(repl, [plugin("evil", plugin_command("zz-" + key, (key,)))])
        if registry.get(key) is not before[key]:
            taken_over.append(key)

    assert taken_over == []
    # A refused alias only drops that alias: the only additions are the differently named
    # commands themselves, and no built-in key points at anything but its built-in.
    assert set(registry._commands) - set(before) == {"zz-" + key for key in keys}
    assert all(registry._commands[key] is before[key] for key in keys)


def test_a_refused_name_refuses_the_whole_command_including_its_aliases(repl):
    register_plugin_commands(repl, [plugin("evil", plugin_command("help", ("evil-alias",)))])
    assert repl._registry.get("evil-alias") is None


def test_a_refused_alias_drops_only_that_alias(repl):
    register_plugin_commands(repl, [plugin("good", plugin_command("mycmd", ("q", "mycmd2")))])
    assert repl._registry.get("mycmd") is not None
    assert repl._registry.get("mycmd2") is not None
    assert repl._registry.get("q").name == "exit"  # the built-in alias is untouched


@pytest.mark.parametrize("builtin", ["help", "exit", "q", "h", "?"])
@pytest.mark.parametrize("spelling", ["{}", "{}".upper(), " {} ", "/{}", "//{}", "\t{}\n"])
def test_spelling_variants_of_a_builtin_are_refused(repl, builtin, spelling):
    key = spelling.format(builtin) if "{}" in spelling else spelling
    original = repl._registry.get(builtin)
    register_plugin_commands(repl, [plugin("evil", plugin_command("x-" + builtin, (key,)))])
    assert repl._registry.get(builtin) is original


def test_mixed_case_variants_are_refused(repl):
    original = repl._registry.get("help")
    for key in ("HELP", "Help", "hElP"):
        register_plugin_commands(repl, [plugin("evil", plugin_command("x", (key,)))])
        assert repl._registry.get("help") is original


# ── reserved names ────────────────────────────────────────────────────────────────────────────


def test_trust_is_reserved_even_though_it_is_not_a_slash_command(repl):
    assert "trust" in RESERVED_COMMAND_NAMES
    assert repl._registry.get("trust") is None  # nothing to shadow: trust is `velune trust`
    for spelling in ("trust", "TRUST", " trust ", "/trust"):
        register_plugin_commands(
            repl, [plugin("evil", plugin_command("x-" + spelling.strip("/ "), (spelling,)))]
        )
        register_plugin_commands(repl, [plugin("evil", plugin_command(spelling))])
    assert repl._registry.get("trust") is None


def test_a_command_file_cannot_take_a_builtin_alias_or_a_reserved_name(repl, user_commands):
    (user_commands / "q.toml").write_text('prompt = "hijack"\ndescription = "x"\n')
    (user_commands / "trust.toml").write_text('prompt = "hijack"\ndescription = "x"\n')
    (user_commands / "mycmd.toml").write_text('prompt = "fine"\ndescription = "x"\n')
    exit_command = repl._registry.get("exit")

    _load_file_commands(repl, repl._registry)

    assert repl._registry.get("q") is exit_command  # used to be replaced
    assert repl._registry.get("trust") is None
    assert repl._registry.get("mycmd") is not None  # a legitimate command file still loads


# ── ordering and duplicates ───────────────────────────────────────────────────────────────────


def test_the_first_plugin_to_register_a_name_keeps_it(repl):
    register_plugin_commands(repl, [plugin("first", plugin_command("deploy"))])
    register_plugin_commands(repl, [plugin("second", plugin_command("deploy"))])
    assert repl._registry.get("deploy").owner == "first"


def test_a_clash_is_decided_by_registration_order_nothing_else(repl):
    register_plugin_commands(repl, [plugin("second", plugin_command("deploy"))])
    register_plugin_commands(repl, [plugin("first", plugin_command("deploy"))])
    assert repl._registry.get("deploy").owner == "second"


def test_an_alias_cannot_take_a_name_another_plugin_holds_in_any_case(repl):
    register_plugin_commands(repl, [plugin("a", plugin_command("deploy"))])
    register_plugin_commands(repl, [plugin("b", plugin_command("other", ("DEPLOY",)))])
    assert repl._registry.get("deploy").owner == "a"


def test_a_plugin_may_register_its_own_command_again_on_reload(repl):
    register_plugin_commands(repl, [plugin("a", plugin_command("deploy"))])
    first = repl._registry.get("deploy")
    register_plugin_commands(repl, [plugin("a", plugin_command("deploy"))])
    assert repl._registry.get("deploy") is not first  # replaced by the same owner
    assert repl._registry.get("deploy").owner == "a"


def test_a_key_registered_before_sealing_is_protected_after_it():
    registry = SlashCommandRegistry()
    registry.register(command("core"))
    registry.seal_builtins()
    assert registry.register(command("core", owner="p")) == ["core"]
    assert registry.get("core").owner == ""


def test_before_sealing_the_last_registration_still_wins_as_it_always_did():
    registry = SlashCommandRegistry()
    registry.register(command("x"))
    registry.register(command("x", aliases=("y",)))
    assert registry.get("y") is not None


# ── dispatch ──────────────────────────────────────────────────────────────────────────────────


async def test_dispatch_runs_the_builtin_not_a_plugin_that_tried_to_replace_it():
    builtin, plugin_handler = AsyncMock(), AsyncMock()
    registry = SlashCommandRegistry()
    registry.register(command("help", handler=builtin))
    registry.seal_builtins()
    registry.register(command("help", owner="evil", handler=plugin_handler))
    registry.register(command("zz", aliases=("HELP",), owner="evil", handler=plugin_handler))
    stub = SimpleNamespace(
        console=MagicMock(),
        _registry=registry,
        _completer=None,
        _interrupts=MagicMock(),
        _refresh_invalid_keys=MagicMock(),
    )

    for typed in ("/help", "/HELP", "/Help arg"):
        await VeluneREPL._handle_slash_command(stub, typed)

    assert builtin.await_count == 3
    plugin_handler.assert_not_awaited()


async def test_a_plugin_command_refuses_to_run_once_its_plugin_is_not_active(repl):
    repl._handle_prompt = AsyncMock()
    manager = MagicMock()
    repl._plugin_manager = manager
    register_plugin_commands(repl, [plugin("p", plugin_command("mycmd"))])
    handler = repl._registry.get("mycmd").handler

    manager.is_active.return_value = False
    await handler("")
    repl._handle_prompt.assert_not_awaited()
    manager.is_active.assert_called_with("p")

    manager.is_active.return_value = True
    await handler("")
    repl._handle_prompt.assert_awaited_once()


# ── removal and normalization ─────────────────────────────────────────────────────────────────


def test_unregister_owner_removes_only_that_owners_keys_and_is_idempotent(repl):
    register_plugin_commands(repl, [plugin("a", plugin_command("one", ("o1",)))])
    register_plugin_commands(repl, [plugin("b", plugin_command("two"))])
    builtins = dict(build_slash_registry(repl)._commands)

    assert sorted(repl._registry.unregister_owner("a")) == ["o1", "one"]
    assert repl._registry.unregister_owner("a") == []
    assert repl._registry.get("one") is None and repl._registry.get("o1") is None
    assert repl._registry.get("two").owner == "b"
    assert all(repl._registry.get(k).name == v.name for k, v in builtins.items())
    assert repl._registry.unregister_owner("") == []  # built-ins are never removed this way


def test_an_alias_is_stored_and_found_under_its_normalized_form(repl):
    register_plugin_commands(repl, [plugin("a", plugin_command("norm", ("Fancy2", " padded ")))])
    assert repl._registry.get("fancy2") is not None
    assert repl._registry.get("FANCY2") is not None
    assert repl._registry.get("padded") is not None


@pytest.mark.parametrize("bad", ["", "   ", "two words", "/", "a\tb"])
def test_a_key_that_cannot_be_typed_is_ignored(repl, bad):
    register_plugin_commands(repl, [plugin("a", plugin_command("mycmd", (bad,)))])
    assert repl._registry.get("mycmd") is not None
    assert bad.strip() == "" or repl._registry.get(bad) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Help", "help"), (" help ", "help"), ("/help", "help"), ("//HELP", "help"), ("?", "?")],
)
def test_normalize_command_key(raw, expected):
    assert normalize_command_key(raw) == expected


# ── hostile data in a command file must not stop anything else registering ─────────────────────


def test_non_text_aliases_are_ignored_not_raised():
    registry = SlashCommandRegistry()
    registry.seal_builtins()
    refused = registry.register(command("ok-cmd", aliases=(1, None, 2.5, "fine", ["x"])))
    assert refused == [1, None, 2.5, ["x"]]  # reported, never raised
    assert registry.get("ok-cmd") is not None and registry.get("fine") is not None


@pytest.mark.parametrize(
    ("frontmatter", "expected"),
    [
        ("aliases: [1, 2, null]", []),
        ("aliases: 5", []),
        ("aliases: {a: b}", []),
        ("aliases: [good, 7, null, fine]", ["good", "fine"]),
        ("aliases: solo", ["solo"]),
    ],
)
def test_frontmatter_aliases_are_only_ever_text(tmp_path, frontmatter, expected):
    from velune.plugins.declarative.command import parse_command_file

    path = tmp_path / "cmd.md"
    path.write_text(f"---\n{frontmatter}\nargs: [1, name]\n---\nbody", encoding="utf-8")
    parsed = parse_command_file(path, "p")
    assert parsed is not None
    assert parsed.aliases == expected
    assert parsed.args == ["name"]


def test_one_plugin_failing_to_register_does_not_stop_the_others(repl, caplog):
    registry = repl._registry
    real = registry.register

    def fussy(cmd):
        if cmd.owner == "bad":
            raise TypeError("simulated bad command data")
        return real(cmd)

    registry.register = fussy
    with caplog.at_level("WARNING", logger="velune.cli.handlers.plugins"):
        register_plugin_commands(
            repl,
            [
                plugin("bad", plugin_command("bad-cmd")),
                plugin("good", plugin_command("good-cmd")),
            ],
        )

    assert registry.get("good-cmd") is not None
    assert registry.get("bad-cmd") is None
    assert "Could not register commands from plugin 'bad'" in caplog.text


# ── the corrected /trust guidance ─────────────────────────────────────────────────────────────


def test_the_trust_prompt_points_at_the_real_command(tmp_path, monkeypatch):
    from rich import prompt

    from velune.core import trust

    workspace = tmp_path / "repo"
    (workspace / ".velune" / "plugins" / "p" / ".velune-plugin").mkdir(parents=True)
    (workspace / ".velune" / "plugins" / "p" / ".velune-plugin" / "plugin.json").write_text(
        '{"name": "p"}'
    )
    monkeypatch.setattr(trust, "is_trusted", lambda path: False)
    monkeypatch.setattr(trust, "trust", lambda path: None)
    monkeypatch.setattr(prompt.Confirm, "ask", lambda *a, **k: True)
    console = MagicMock()
    stub = SimpleNamespace(_mcp_registry=SimpleNamespace(workspace=workspace), console=console)

    assert VeluneREPL._ensure_workspace_trust(stub) is True

    printed = " ".join(str(call.args[0]) for call in console.print.call_args_list)
    assert "velune trust forget" in printed
    assert "/trust" not in printed  # there is no such slash command
