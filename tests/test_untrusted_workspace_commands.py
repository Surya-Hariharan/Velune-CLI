"""A repository must not be able to ship slash commands that run without the user's trust.

``<workspace>/.velune/commands/*.toml`` can define a ``shell`` command. Project hooks, project MCP
servers and project ``base_url`` overrides are all gated on the per-directory trust decision
(:mod:`velune.core.trust`); these files were not, so a cloned repository could define an innocuous
looking ``/test`` or ``/lint`` that runs an arbitrary shell command, with the user's environment,
the first time it was typed. They now load only for a trusted workspace.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from velune.cli.commands import file_commands
from velune.cli.commands.file_commands import FileCommandLoader, has_workspace_commands

MARKER = "ran.txt"


def _write_command(directory: Path, name: str, command: str, description: str = "do it") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.toml").write_text(
        f'description = "{description}"\n[action]\ntype = "shell"\ncommand = "{command}"\n',
        encoding="utf-8",
    )


@pytest.fixture
def user_dir(tmp_path, monkeypatch) -> Path:
    """An empty user-level commands folder, so the real home directory is never read."""
    path = tmp_path / "home" / ".velune" / "commands"
    path.mkdir(parents=True)
    monkeypatch.setattr(file_commands, "_USER_COMMANDS_DIR", path)
    return path


@pytest.fixture
def workspace(tmp_path) -> Path:
    ws = tmp_path / "repo"
    _write_command(ws / ".velune" / "commands", "test", f"echo hi > {MARKER}")
    return ws


def _names(commands) -> list[str]:
    return sorted(c.name for c in commands)


def _console():
    return SimpleNamespace(print=lambda *a, **k: None)


# ── the loader ─────────────────────────────────────────────────────────────────────────────────


def test_an_untrusted_workspace_contributes_no_commands(workspace, user_dir):
    loader = FileCommandLoader(workspace=workspace)  # trust is off unless asked for
    assert loader.load(_console(), existing_names=set()) == []


def test_an_explicitly_untrusted_workspace_contributes_no_commands(workspace, user_dir):
    loader = FileCommandLoader(workspace=workspace, trusted=False)
    assert loader.load(_console(), existing_names=set()) == []


def test_a_trusted_workspace_contributes_its_commands(workspace, user_dir):
    loader = FileCommandLoader(workspace=workspace, trusted=True)
    assert _names(loader.load(_console(), existing_names=set())) == ["test"]


def test_user_commands_load_whatever_the_workspace_trust(workspace, user_dir):
    _write_command(user_dir, "mine", "echo mine")
    for trusted in (False, True):
        loaded = FileCommandLoader(workspace=workspace, trusted=trusted).load(
            _console(), existing_names=set()
        )
        assert "mine" in _names(loaded)


def test_an_untrusted_workspace_cannot_replace_a_user_command(workspace, user_dir):
    """Workspace commands override same-named user commands only once trusted."""
    _write_command(user_dir, "test", "echo the-users-own", description="the user's own")
    untrusted = FileCommandLoader(workspace=workspace).load(_console(), existing_names=set())
    assert [c.description for c in untrusted if c.name == "test"][0].startswith("the user's own")
    trusted = FileCommandLoader(workspace=workspace, trusted=True).load(
        _console(), existing_names=set()
    )
    assert [c.description for c in trusted if c.name == "test"][0].startswith("do it")


def test_built_in_commands_are_still_never_shadowed(workspace, user_dir):
    loader = FileCommandLoader(workspace=workspace, trusted=True)
    assert loader.load(_console(), existing_names={"test"}) == []


def test_no_workspace_means_only_user_commands(user_dir):
    _write_command(user_dir, "mine", "echo mine")
    assert _names(FileCommandLoader(workspace=None).load(_console(), set())) == ["mine"]


async def test_the_command_does_not_run_from_an_untrusted_workspace(
    workspace, user_dir, tmp_path, monkeypatch
):
    """The proof of concept: typing the repository's ``/test`` must not run its shell command."""
    monkeypatch.chdir(tmp_path)
    loaded = FileCommandLoader(workspace=workspace).load(_console(), existing_names=set())
    assert loaded == []  # there is nothing to type, so nothing can run
    assert not (tmp_path / MARKER).exists()


async def test_the_command_runs_from_a_trusted_workspace(
    workspace, user_dir, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    loaded = FileCommandLoader(workspace=workspace, trusted=True).load(
        _console(), existing_names=set()
    )
    await loaded[0].handler("")
    assert (tmp_path / MARKER).exists()


def test_has_workspace_commands(workspace, tmp_path):
    assert has_workspace_commands(workspace)
    assert not has_workspace_commands(tmp_path / "empty")
    (tmp_path / "other" / ".velune" / "commands").mkdir(parents=True)
    assert not has_workspace_commands(tmp_path / "other")  # a folder without .toml files


# ── the registry hook ──────────────────────────────────────────────────────────────────────────


class _Registry:
    def __init__(self, names=()):
        self.commands = {n: object() for n in names}

    def all_unique(self):
        return [SimpleNamespace(name=n) for n in self.commands]

    def register(self, command):
        self.commands[command.name] = command


def _repl(workspace: Path):
    container = SimpleNamespace(get=lambda key: str(workspace))
    return SimpleNamespace(container=container, console=_console())


@pytest.mark.parametrize("stored_decision", [False, True])
def test_the_registry_follows_the_stored_trust_decision(
    workspace, user_dir, monkeypatch, stored_decision
):
    from velune.cli.slash_dispatcher import _load_file_commands
    from velune.core import trust

    monkeypatch.setattr(trust, "is_trusted", lambda path: stored_decision)
    registry = _Registry()
    _load_file_commands(_repl(workspace), registry)
    assert ("test" in registry.commands) is stored_decision


def test_a_decision_made_after_startup_adds_the_commands(workspace, user_dir, monkeypatch):
    from velune.cli.slash_dispatcher import _load_file_commands
    from velune.core import trust

    monkeypatch.setattr(trust, "is_trusted", lambda path: False)
    registry = _Registry()
    _load_file_commands(_repl(workspace), registry)
    assert "test" not in registry.commands
    _load_file_commands(_repl(workspace), registry, trusted=True)  # the user just trusted it
    assert "test" in registry.commands


# ── the trust prompt ───────────────────────────────────────────────────────────────────────────


def _ask_for_trust(workspace: Path, monkeypatch, answers: list):
    from rich import prompt

    from velune.cli.repl import VeluneREPL
    from velune.core import trust

    monkeypatch.setattr(trust, "is_trusted", lambda path: False)
    monkeypatch.setattr(trust, "trust", lambda path: answers.append(("trusted", path)))
    monkeypatch.setattr(prompt.Confirm, "ask", lambda *a, **k: answers.append(("asked",)) or False)
    stub = SimpleNamespace(_mcp_registry=SimpleNamespace(workspace=workspace), console=_console())
    return VeluneREPL._ensure_workspace_trust(stub)


def test_a_workspace_with_only_slash_commands_prompts_for_trust(workspace, monkeypatch):
    answers: list = []
    assert _ask_for_trust(workspace, monkeypatch, answers) is False
    assert ("asked",) in answers  # it used to be silently treated as "nothing to trust"
    assert not [a for a in answers if a[0] == "trusted"]


def test_a_workspace_without_project_config_does_not_prompt(tmp_path, monkeypatch):
    (tmp_path / "plain").mkdir()
    answers: list = []
    assert _ask_for_trust(tmp_path / "plain", monkeypatch, answers) is False
    assert answers == []
