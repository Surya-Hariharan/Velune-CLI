"""Tests for the discoverability / visibility / workflow UX layer.

Covers the three coordinated workstreams:
  * Visibility — ``SystemSnapshot`` composition + the extended ``ProgressDashboard``.
  * Workflow   — the ``next_steps`` footer primitive + the ``guidance`` map.
  * Discoverability — intent-based palette search + the ``/status`` alias.
"""

from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console

from velune.cli import guidance, ui


def _recording_console(width: int = 100) -> Console:
    """A Console that records to an in-memory buffer (UTF-8, no terminal)."""
    return Console(record=True, width=width, file=io.StringIO())


# ---------------------------------------------------------------------------
# Workflow — guidance map + next_steps footer
# ---------------------------------------------------------------------------


def test_guidance_fills_placeholders():
    steps = guidance.steps_for("provider_added", model="gpt-4o")
    assert steps, "known outcome should yield steps"
    # The {model} placeholder is substituted into the suggested command.
    assert any("gpt-4o" in command for _, command, _ in steps)
    # Guidance is surfaced to a shell by CLI commands, so it must be CLI-native.
    assert any(command == "velune models scan" for _, command, _ in steps)


def test_guidance_unknown_outcome_is_empty():
    assert guidance.steps_for("does-not-exist") == []


def test_guidance_missing_placeholder_keeps_template():
    # No value provided for {model} → template kept rather than crashing.
    steps = guidance.steps_for("provider_added")
    assert steps
    assert any("{model}" in command for _, command, _ in steps)


def test_next_steps_renders_summary_and_commands():
    steps = guidance.steps_for("models_scanned", model="llama3")
    console = _recording_console()
    console.print(ui.next_steps("Models discovered", "5 models ready.", steps))
    out = console.export_text()
    assert "Models discovered" in out
    assert "5 models ready." in out
    assert "velune models use llama3" in out
    assert "velune models benchmark" in out


# ---------------------------------------------------------------------------
# Visibility — SystemSnapshot composition
# ---------------------------------------------------------------------------


def test_system_snapshot_uses_passed_counts(tmp_path: Path):
    from velune.cli.display.system_snapshot import build_system_snapshot

    snap = build_system_snapshot(tmp_path, plugin_count=4, mcp_count=2, session_count=7)
    # Counts come straight from the caller's registries — no invention.
    assert snap.integrations.plugins == 4
    assert snap.integrations.mcp_servers == 2
    assert snap.integrations.sessions == 7
    # A bare temp dir has no index — truthful zeroed state, not a guess.
    assert snap.index.exists is False
    assert snap.index.files == 0
    assert snap.index.freshness == "no-index"
    assert snap.workspace == str(tmp_path.resolve())


def test_system_snapshot_matches_context_report(tmp_path: Path):
    """Snapshot fields must mirror the underlying context report (single truth)."""
    from velune.cli.display.system_snapshot import build_system_snapshot
    from velune.observability.context_report import build_context_report

    report = build_context_report(tmp_path)
    snap = build_system_snapshot(tmp_path)
    assert snap.index.files == report.indexed_file_count
    assert snap.index.symbols == report.total_symbols
    assert snap.index.freshness == report.freshness
    assert snap.memory_tables == report.memory_tables
    assert snap.health == report.health


# ---------------------------------------------------------------------------
# Visibility — extended dashboard layout
# ---------------------------------------------------------------------------


def test_dashboard_renders_session_and_state_panels(tmp_path: Path):
    from velune.cli.display.dashboard import ProgressDashboard
    from velune.cli.display.system_snapshot import (
        LiveSessionState,
        build_system_snapshot,
    )

    snap = build_system_snapshot(tmp_path, plugin_count=1, mcp_count=2, session_count=3)
    live = LiveSessionState(
        model_id="gpt-4o", mode_label="NORMAL", context_pct=31.0, provider_id="openai"
    )
    dash = ProgressDashboard(
        console=_recording_console(),
        job_registry=None,
        alert_store=None,
        health_monitor=None,
        snapshot=snap,
        live_state=lambda: live,
    )
    console = _recording_console()
    console.print(dash._build_layout())
    out = console.export_text()
    for label in ("Session", "Index", "Memory", "Integrations"):
        assert label in out, f"missing dashboard panel: {label}"
    assert "gpt-4o" in out
    assert "openai" in out


def test_dashboard_static_snapshot_built_once(tmp_path: Path, monkeypatch):
    """The expensive snapshot must not be rebuilt on every refresh tick."""
    from velune.cli.display import system_snapshot
    from velune.cli.display.dashboard import ProgressDashboard
    from velune.cli.display.system_snapshot import LiveSessionState

    snap = system_snapshot.build_system_snapshot(tmp_path)
    live_calls = {"n": 0}

    def _live() -> LiveSessionState:
        live_calls["n"] += 1
        return LiveSessionState(model_id="m", mode_label="NORMAL", context_pct=0.0)

    dash = ProgressDashboard(
        console=_recording_console(),
        job_registry=None,
        alert_store=None,
        health_monitor=None,
        snapshot=snap,
        live_state=_live,
    )
    # Multiple refreshes reuse the same cached snapshot object…
    for _ in range(3):
        dash._build_layout()
    assert dash._snapshot is snap
    # …while live state is re-read each tick.
    assert live_calls["n"] >= 3


def test_dashboard_without_snapshot_keeps_legacy_layout():
    from velune.cli.display.dashboard import ProgressDashboard

    dash = ProgressDashboard(
        console=_recording_console(),
        job_registry=None,
        alert_store=None,
        health_monitor=None,
    )
    console = _recording_console()
    console.print(dash._build_layout())
    out = console.export_text()
    assert "Session" not in out  # no session band when no snapshot supplied
    assert "Background Jobs" in out


# ---------------------------------------------------------------------------
# Discoverability — intent search + /status alias
# ---------------------------------------------------------------------------


class _StubREPL:
    """Stub that returns a noop coroutine for any ``_cmd_*`` handler attribute."""

    def __getattr__(self, _name):
        async def _noop(*_a, **_k):
            return None

        return _noop


def _build_registry():
    from velune.cli.slash_dispatcher import build_slash_registry

    return build_slash_registry(_StubREPL())


def _palette_model():
    # The palette only needs command metadata (name/aliases/search_terms/
    # description/category), so a stubbed handler registry is sufficient.
    from velune.cli.command_palette import CommandPaletteModel

    return CommandPaletteModel(_build_registry().all_unique())


def test_status_resolves_to_dashboard():
    registry = _build_registry()
    assert registry.get("status") is registry.get("dashboard")


def test_palette_finds_dashboard_by_intent():
    model = _palette_model()
    for query in ("status", "overview", "state"):
        matches = model.matches(query)
        names = {m.command.name for m in matches}
        assert "dashboard" in names, f"'{query}' should surface the dashboard"


def test_palette_finds_providers_by_credentials():
    model = _palette_model()
    matches = model.matches("credentials")
    names = {m.command.name for m in matches}
    assert "providers" in names
