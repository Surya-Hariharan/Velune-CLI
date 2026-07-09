"""Regression tests for the onboarding package's public surface.

The `velune/cli/onboarding/__init__.py` re-exports below are exactly the
names `commands/onboard.py`, `commands/setup.py`, and `app.py` import — a
missing/renamed export here previously shipped as a silent `ImportError` on
every interactive first run because nothing exercised these import paths.
"""

from __future__ import annotations

import inspect

import pytest
from typer.testing import CliRunner

from velune.cli.app import create_app

runner = CliRunner()


@pytest.fixture
def app():
    return create_app(register="__all__")


def test_onboarding_public_names_importable():
    from velune.cli.onboarding import (
        _STAGE_NAMES,
        load_stage_progress,
        onboarding_state,
        run_onboarding,
    )

    assert isinstance(_STAGE_NAMES, tuple)
    assert len(_STAGE_NAMES) == 8
    assert callable(load_stage_progress)
    assert callable(onboarding_state)
    assert callable(run_onboarding)


def test_run_onboarding_signature():
    from velune.cli.onboarding import run_onboarding

    sig = inspect.signature(run_onboarding)
    params = list(sig.parameters)
    assert params[0] == "runtime"
    assert sig.parameters["start_stage"].default == 0


def test_run_onboarding_is_sync_wrapper_around_async_stages_run():
    from velune.cli.onboarding import run_onboarding, stages

    assert not inspect.iscoroutinefunction(run_onboarding)
    assert inspect.iscoroutinefunction(stages.run)


def test_onboard_command_rejects_non_interactive(app):
    # CliRunner's isolated stdin/stdout are never real TTYs, so the guard
    # added for spec item 4 (never hang on piped input) should fire.
    result = runner.invoke(app, ["onboard"])
    assert result.exit_code != 0
    assert "interactive terminal" in result.output


def test_setup_command_rejects_non_interactive(app):
    result = runner.invoke(app, ["setup"])
    assert result.exit_code != 0
    assert "interactive terminal" in result.output
