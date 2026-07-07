"""Workspace pillar: Remember + Resume.

Covers the three gaps closed in the Workspace deep pass:

* ``WorkspaceRegistry.touch`` remembers a workspace even if it was never
  explicitly ``open``ed (the "Remember real usage" contract).
* ``open``/``resume`` resolve a *registered name* as well as a path.
* ``velune workspace resume`` reopens the most-recent workspace and surfaces its
  latest saved session so work continues where it left off.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import typer

from velune.cli import sessions as sessions_mod
from velune.cli import workspaces as ws_mod
from velune.cli.commands import workspace as ws_cmd
from velune.cli.context import CLIContext
from velune.cli.workspaces import WorkspaceRegistry


def _reg(tmp_path) -> WorkspaceRegistry:
    return WorkspaceRegistry(path=tmp_path / "workspaces.json")


# ── Store contract ───────────────────────────────────────────────────────────


def test_touch_registers_unknown_workspace(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    reg = _reg(tmp_path)
    assert reg.get(proj) is None

    reg.touch(proj)

    info = reg.get(proj)
    assert info is not None
    assert info.name == "proj"


def test_touch_updates_recency_order(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    reg = _reg(tmp_path)

    reg.register(a)
    reg.register(b)
    # b registered last → most recent.
    assert reg.list()[0].name == "b"

    reg.touch(a)
    # touching a bumps it to the front.
    assert reg.list()[0].name == "a"


def test_list_prunes_deleted_paths(tmp_path):
    gone = tmp_path / "gone"
    gone.mkdir()
    reg = _reg(tmp_path)
    reg.register(gone)
    gone.rmdir()
    assert reg.list() == []


# ── Name-or-path resolution ──────────────────────────────────────────────────


def test_resolve_target_by_name(tmp_path):
    proj = tmp_path / "myproj"
    proj.mkdir()
    reg = _reg(tmp_path)
    reg.register(proj)

    resolved = ws_cmd._resolve_workspace_target(reg, "myproj")
    assert resolved is not None
    assert resolved.name == "myproj"


def test_resolve_target_by_path(tmp_path):
    proj = tmp_path / "bypath"
    proj.mkdir()
    reg = _reg(tmp_path)

    resolved = ws_cmd._resolve_workspace_target(reg, str(proj))
    assert resolved is not None
    assert resolved.name == "bypath"


def test_resolve_target_unknown_returns_none(tmp_path):
    reg = _reg(tmp_path)
    assert ws_cmd._resolve_workspace_target(reg, "nope-not-here") is None


# ── resume command ───────────────────────────────────────────────────────────


@pytest.fixture
def _isolate(tmp_path, monkeypatch):
    """Point the registry and session store at the test's tmp dirs."""
    monkeypatch.setattr(ws_mod, "DEFAULT_REGISTRY_PATH", tmp_path / "workspaces.json")
    monkeypatch.setattr(sessions_mod, "DEFAULT_SESSIONS_DIR", tmp_path / "sessions")
    return tmp_path


def _ctx(tmp_path):
    runtime = SimpleNamespace(container=SimpleNamespace(get=lambda k: None))
    cli_context = CLIContext(
        workspace=tmp_path,
        config_path=None,
        verbose=False,
        runtime=runtime,
        json_mode=True,
    )
    return SimpleNamespace(obj=cli_context)


def test_resume_no_arg_picks_most_recent(_isolate, capsys):
    tmp_path = _isolate
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    reg = WorkspaceRegistry()
    reg.register(a)
    reg.register(b)  # most recent

    ws_cmd.workspace_resume(_ctx(tmp_path), name=None)

    out = json.loads(capsys.readouterr().out)
    assert out["name"] == "b"
    assert out["latest_session"] is None


def test_resume_surfaces_latest_session(_isolate, capsys):
    tmp_path = _isolate
    proj = tmp_path / "withsess"
    proj.mkdir()
    reg = WorkspaceRegistry()
    reg.register(proj)

    store = sessions_mod.SessionStore()
    store.save(
        [{"role": "user", "content": "resume me"}],
        workspace=str(proj.resolve()),
        model_id="m",
        session_id="sess0001",
    )

    ws_cmd.workspace_resume(_ctx(tmp_path), name="withsess")

    out = json.loads(capsys.readouterr().out)
    assert out["name"] == "withsess"
    assert out["latest_session"]["id"] == "sess0001"


def test_resume_nothing_registered_exits_nonzero(_isolate):
    tmp_path = _isolate
    with pytest.raises(typer.Exit) as exc:
        ws_cmd.workspace_resume(_ctx(tmp_path), name=None)
    assert exc.value.exit_code == 1


# ── Remember-on-use bootstrap gating ─────────────────────────────────────────


def test_bootstrap_remembers_only_indexed_workspaces(_isolate):
    """Running a subcommand registers an *indexed* workspace, not a plain dir.

    The gate is `.velune/index` (written by `velune init`), because the runtime
    itself creates `.velune/snapshots` — so a bare `.velune` must not count.
    """
    from typer.testing import CliRunner

    from velune.cli.app import create_app

    tmp_path = _isolate
    plain = tmp_path / "plain"
    plain.mkdir()
    indexed = tmp_path / "indexed"
    (indexed / ".velune" / "index").mkdir(parents=True)

    runner = CliRunner()
    app = create_app(register="__all__")
    runner.invoke(app, ["--workspace", str(plain), "--json", "usage"])
    runner.invoke(app, ["--workspace", str(indexed), "--json", "usage"])

    names = {w.name for w in WorkspaceRegistry().list()}
    assert "indexed" in names
    assert "plain" not in names
