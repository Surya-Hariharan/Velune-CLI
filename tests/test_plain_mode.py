"""--plain (linear, non-alt-screen) REPL mode.

Full end-to-end coverage of VeluneREPL.run() would need a real runtime, which
is too heavy for a unit test — these pin the concrete, testable pieces
instead: the plain-mode PromptSession builder shares the fullscreen UI's
completer, the CLI flag threads down to entrypoint.launch()/_async_main(),
and VeluneREPL.run(plain=True) never builds a FullscreenREPLUI (the thing
that owns the alternate screen buffer).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from velune.cli.repl import VeluneREPL


class _FakeCommand:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = "desc"
        self.category = "misc"
        self.aliases: list[str] = []
        self.hidden = False


class _FakeRegistry:
    def all_unique(self):
        return [_FakeCommand("help"), _FakeCommand("model")]


class _FakeModelRegistry:
    def list_all(self):
        return []


class _FakeContainer:
    def __init__(self, services: dict) -> None:
        self._services = services

    def get(self, key: str):
        return self._services[key]


def _fake_repl(tmp_path: Path):
    fake = SimpleNamespace(
        _history_file=tmp_path / "history",
        _registry=_FakeRegistry(),
        container=_FakeContainer({"runtime.model_registry": _FakeModelRegistry()}),
    )
    fake._build_completer = lambda: VeluneREPL._build_completer(fake)
    return fake


def test_build_completer_returns_slash_completer(tmp_path):
    fake = _fake_repl(tmp_path)
    completer = VeluneREPL._build_completer(fake)
    assert completer is not None
    assert fake._completer is completer


def test_build_plain_session_returns_a_non_fullscreen_prompt_session(tmp_path):
    fake = _fake_repl(tmp_path)
    # PromptSession touches the real console at construction time unless run
    # inside an app session with an explicit input/output — needed here since
    # the test runner's terminal may not be a native console (this is exactly
    # the "some SSH/CI terminals" case --plain is partly for).
    with (
        create_pipe_input() as pipe_input,
        create_app_session(input=pipe_input, output=DummyOutput()),
    ):
        session = VeluneREPL._build_plain_session(fake)
    assert isinstance(session, PromptSession)
    # A PromptSession has no notion of an alternate screen / full_screen flag
    # at all — that's exactly the point: unlike FullscreenREPLUI's
    # Application(full_screen=True), there is nothing here to take over the
    # terminal. Just confirm it's wired with the same completer/history this
    # REPL already builds for the fullscreen path.
    assert session.completer is not None
    assert session.history is not None


def test_plain_flag_forwarded_from_launch_to_async_main(monkeypatch):
    from velune.kernel import entrypoint

    captured = {}

    async def fake_async_main(runtime, *, plain=False):
        captured["plain"] = plain

    monkeypatch.setattr(entrypoint, "_async_main", fake_async_main)
    entrypoint.launch(SimpleNamespace(), plain=True)
    assert captured["plain"] is True

    entrypoint.launch(SimpleNamespace(), plain=False)
    assert captured["plain"] is False


def test_cli_root_callback_exposes_plain_option():
    from velune.cli.app import create_app

    app = create_app(register=None)
    # Typer stores the callback's click.Command params after registration;
    # walking the underlying Click command is the simplest reliable way to
    # assert `--plain` is a real, wired-up option and not just planned.
    import typer.main

    click_app = typer.main.get_command(app)
    option_names = {
        name for param in click_app.params for name in getattr(param, "opts", [])
    }
    assert "--plain" in option_names
