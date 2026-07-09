"""Tests for the automatic 'Repository Detected' on-entry indexing flow.

Phase-12-style requirement: entering a project should show a detection banner
and index in the background, without blocking the prompt or requiring the
user to run /cognition manually. Reconciled with the codebase's existing
"launch must stay instant" constraint by using only the already-fast
manifest-only quick_summary() and firing the actual index as a background
job via the existing job-registry machinery (never blocking).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from velune.cli.handlers.cognition import auto_detect_on_entry


def _make_repl(*, cognition=None, job_registry=MagicMock(), config=None):
    repl = MagicMock()
    repl.container.get.side_effect = lambda key: {
        "runtime.repository_cognition": cognition,
        "runtime.config": config,
    }.get(key)
    repl._job_registry = job_registry
    repl.console = MagicMock()
    return repl


def _cognition(*, unsafe=None, summary=None):
    cog = MagicMock()
    cog.unsafe_reason.return_value = unsafe
    cog.quick_summary.return_value = summary or {}
    return cog


async def test_skips_when_cognition_service_unavailable():
    repl = _make_repl(cognition=None)
    await auto_detect_on_entry(repl)
    repl.console.print.assert_not_called()


async def test_skips_when_job_registry_unavailable_to_avoid_blocking():
    cog = _cognition(summary={"project_type": "Python"})
    repl = _make_repl(cognition=cog, job_registry=None)
    await auto_detect_on_entry(repl)
    repl.console.print.assert_not_called()


async def test_skips_workspaces_that_are_unsafe_to_index():
    cog = _cognition(unsafe="your home directory")
    repl = _make_repl(cognition=cog)
    await auto_detect_on_entry(repl)
    cog.quick_summary.assert_not_called()
    repl.console.print.assert_not_called()


async def test_skips_when_index_on_init_disabled():
    config = MagicMock()
    config.workspace.index_on_init = False
    cog = _cognition(summary={"project_type": "Python"})
    repl = _make_repl(cognition=cog, config=config)

    await auto_detect_on_entry(repl)

    cog.quick_summary.assert_not_called()
    repl.console.print.assert_not_called()


async def test_stays_quiet_in_a_directory_with_no_recognizable_project():
    cog = _cognition(summary={})  # no project_type, no tech_stack
    repl = _make_repl(cognition=cog)

    await auto_detect_on_entry(repl)

    repl.console.print.assert_not_called()


async def test_renders_banner_and_submits_a_background_index_job(monkeypatch):
    cog = _cognition(summary={"project_type": "Python", "tech_stack": {"frameworks": ["FastAPI"]}})
    repl = _make_repl(cognition=cog)

    submitted = AsyncMock()
    monkeypatch.setattr("velune.cli.handlers.cognition._submit_cognition_job", submitted)

    await auto_detect_on_entry(repl)

    repl.console.print.assert_called()
    banner_text = repl.console.print.call_args_list[0][0][0]
    assert "Repository detected" in banner_text
    assert "Python" in banner_text
    assert "FastAPI" in banner_text

    submitted.assert_awaited_once_with(repl, cog, deep=False, silent=True)


async def test_missing_config_defaults_to_index_on_init_true(monkeypatch):
    """Config lookup failing must never silently disable the feature it gates."""
    cog = _cognition(summary={"project_type": "Python"})
    repl = _make_repl(cognition=cog, config=None)  # container.get("runtime.config") -> None,
    # but MagicMock() default for .workspace.index_on_init on a bare None would AttributeError —
    # _workspace_cognition_settings must catch that and default to (True, True).

    submitted = AsyncMock()
    monkeypatch.setattr("velune.cli.handlers.cognition._submit_cognition_job", submitted)

    await auto_detect_on_entry(repl)

    submitted.assert_awaited_once()
