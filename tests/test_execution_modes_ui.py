"""Execution-mode UX: startup value, switching, Shift+Tab cycle, /approve alias, status badge."""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from rich.console import Console

from velune.cli.execution_modes import (
    BADGES,
    cycle_execution_mode,
    initial_mode,
    parse_mode,
    set_execution_mode,
)
from velune.cli.statusbar import StatusBarState, render_status_bar
from velune.permissions import ExecutionMode


def _container(**values):
    return SimpleNamespace(has=lambda k: k in values, get=lambda k: values[k])


def _config(mode=None):
    return SimpleNamespace(execution=SimpleNamespace(mode=mode) if mode else SimpleNamespace())


def _repl(mode=ExecutionMode.MANUAL):
    buf = io.StringIO()
    repl = SimpleNamespace(
        console=Console(file=buf, width=120, color_system=None),
        _execution_mode=mode,
        _status_state=StatusBarState(),
        _permission_gate=None,
    )
    repl.output = buf
    return repl


def test_default_is_manual():
    assert initial_mode(_container(), _config()) is ExecutionMode.MANUAL


def test_config_sets_the_default():
    assert initial_mode(_container(), _config("auto")) is ExecutionMode.AUTO


def test_cli_flag_overrides_config():
    container = _container(**{"runtime.execution_mode": "plan"})
    assert initial_mode(container, _config("auto")) is ExecutionMode.PLAN


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("manual", ExecutionMode.MANUAL),
        ("AUTO", ExecutionMode.AUTO),
        ("plan", ExecutionMode.PLAN),
        ("ask", ExecutionMode.MANUAL),  # legacy /approve values
        ("safe", ExecutionMode.AUTO),
        ("block", ExecutionMode.PLAN),
        ("yolo", None),
    ],
)
def test_parse_mode(value, expected):
    assert parse_mode(value) is expected


def test_switching_announces_what_changed_and_updates_gate_and_badge():
    from velune.permissions import PolicyState

    repl = _repl()
    repl._permission_gate = SimpleNamespace(state=PolicyState())
    set_execution_mode(repl, ExecutionMode.AUTO)
    text = repl.output.getvalue()
    assert "MANUAL → AUTO" in text
    assert "Workspace boundary remains active" in text
    assert "High-risk operations still require confirmation" in text
    assert repl._execution_mode is ExecutionMode.AUTO
    assert repl._permission_gate.state.mode is ExecutionMode.AUTO
    assert repl._status_state.execution_label == BADGES[ExecutionMode.AUTO]


def test_plan_switch_explains_plan_workflow():
    repl = _repl()
    set_execution_mode(repl, ExecutionMode.PLAN)
    text = repl.output.getvalue()
    assert ".velune/plans/" in text
    assert "approve" in text


def test_shift_tab_cycles_quietly():
    repl = _repl()
    assert cycle_execution_mode(repl) is ExecutionMode.PLAN
    assert cycle_execution_mode(repl) is ExecutionMode.AUTO
    assert cycle_execution_mode(repl) is ExecutionMode.MANUAL
    assert repl.output.getvalue() == ""  # the status bar shows it; no transcript noise


def test_status_bar_shows_the_execution_mode():
    state = StatusBarState(model_id="m", execution_label=BADGES[ExecutionMode.AUTO])
    text = "".join(fragment for _, fragment in render_status_bar(state))
    assert "AUTO" in text
    assert "NORMAL" in text  # the separate fast/normal/max tier is still shown


async def test_approve_alias_maps_onto_modes():
    from velune.cli.handlers.settings import cmd_approve

    repl = _repl()
    await cmd_approve(repl, "safe")
    assert repl._execution_mode is ExecutionMode.AUTO
    await cmd_approve(repl, "block")
    assert repl._execution_mode is ExecutionMode.PLAN
    await cmd_approve(repl, "ask")
    assert repl._execution_mode is ExecutionMode.MANUAL


def test_mode_commands_are_registered_in_the_execution_category():
    from unittest.mock import MagicMock

    from velune.cli.slash_dispatcher import build_slash_registry

    registry = build_slash_registry(MagicMock())
    by_name = {c.name: c for c in registry.all_unique()}
    for name in ("manual", "plan", "auto", "approve"):
        assert by_name[name].category == "Execution"


def test_config_field_validates():
    from velune.kernel.config import ExecutionConfig

    assert ExecutionConfig().mode == "manual"
    with pytest.raises(ValueError):
        ExecutionConfig(mode="yolo")
