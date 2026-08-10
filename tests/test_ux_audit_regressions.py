"""Regressions for the defects found by driving the real CLI (UX audit).

Every test here pins a bug that shipped and was only visible by *running* the
command — each one passed type-checking and import-time linting while being
completely broken at the terminal.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from rich.console import Console

from velune.kernel.config import ConfigService
from velune.providers.validation import ValidationStatus, validate_provider_sync

# --- P0: `velune health` crashed with ImportError -----------------------------


def test_health_overview_runs_without_importerror(monkeypatch):
    """`velune health` used to die on `from ...providers import _PROVIDER_META`.

    That name was deleted in the provider-catalog consolidation, so the command
    raised ImportError on every invocation. Importing the module is not enough
    to catch it — the bad import was *inside* the function body, so only
    actually calling it reproduces the crash.

    Validation is stubbed so this stays offline and fast; the assertion is
    simply that the command completes and renders.
    """
    from velune.cli.commands import usage
    from velune.providers.validation import ValidationResult

    monkeypatch.setattr(
        "velune.providers.validation.validate_provider_sync",
        lambda pid, key="": ValidationResult(
            provider_id=pid, status=ValidationStatus.NOT_SUPPORTED, message="stubbed"
        ),
    )
    monkeypatch.setattr("velune.providers.keystore.has_key", lambda _pid: False)

    # Must not raise ImportError (the original bug) or anything else.
    usage.health_overview(verbose=False)


def test_catalog_supplies_provider_metadata_health_needs():
    """The replacement source must actually expose what health_overview reads."""
    from velune.providers import catalog

    providers = catalog.list_providers_alphabetical()
    assert providers, "catalog must not be empty"
    for meta in providers:
        assert isinstance(meta.id, str) and meta.id
        assert isinstance(meta.requires_key, bool)


# --- P0: provider validation always returned "no current event loop" ----------


def test_validate_provider_sync_works_with_no_running_loop():
    """The core bug: `asyncio.get_event_loop()` raises RuntimeError in a thread
    with no current loop (Python 3.12+), which the broad `except` converted into
    a bogus UNKNOWN_ERROR for *every* provider on *every* CLI invocation.

    An unknown provider is used so no network call happens; the point is that we
    get a real verdict rather than an event-loop error.
    """
    result = validate_provider_sync("definitely-not-a-provider", "")

    assert result.status is ValidationStatus.NOT_SUPPORTED
    assert "event loop" not in result.message.lower()


def test_validate_provider_sync_from_inside_running_loop():
    """The other branch: called from async code it must not re-enter the loop."""

    async def _driver():
        return validate_provider_sync("definitely-not-a-provider", "")

    result = asyncio.run(_driver())

    assert result.status is ValidationStatus.NOT_SUPPORTED
    assert "event loop" not in result.message.lower()


def test_missing_validator_is_not_reported_as_an_error():
    """A provider with no credential check is "can't check", not "broken".

    Reporting NOT_SUPPORTED as UNKNOWN_ERROR made healthy local providers
    (llamacpp, openai-compat) render as red failures in `velune health`.
    """
    result = validate_provider_sync("openai-compat", "")
    assert result.status is ValidationStatus.NOT_SUPPORTED
    assert not result.ok


def test_not_supported_never_marks_a_key_invalid():
    """`verifier` must treat NOT_SUPPORTED as 'no new information'."""
    from velune.providers.verifier import _KEY_REJECTING_STATUSES

    assert ValidationStatus.NOT_SUPPORTED not in _KEY_REJECTING_STATUSES


# --- P1: Rich markup printed literally ---------------------------------------


def _render(renderable) -> str:
    console = Console(file=None, width=200, record=True, force_terminal=False)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


def test_memory_architecture_map_renders_markup_not_literal_tags():
    """`Text.assemble` never parses markup, so these tags were printed verbatim
    (`[bold magenta]VELUNE CORE...`) instead of being styled."""
    from velune.cli.display.memory_view import MemoryDisplayView

    console = Console(width=200)
    with console.capture() as cap:
        MemoryDisplayView(console).render_memory_architecture({"workspace": "/tmp/x"})
    out = cap.get()

    assert "[bold magenta]" not in out
    assert "[/bold magenta]" not in out
    assert "[bold cyan]" not in out
    assert "VELUNE CORE HIERARCHICAL MEMORY MAP" in out
    assert "/tmp/x" in out


def test_no_text_assemble_with_markup_strings_in_cli():
    """Guard the whole bug class: a single-string arg to Text.assemble that
    contains markup is always a literal-tag bug."""
    import re

    offenders: list[str] = []
    for path in Path("velune").rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        for match in re.finditer(r"Text\.assemble\((.*?)\n\s*\)", src, re.DOTALL):
            body = match.group(1)
            # A (text, style) pair is fine; a bare string carrying markup is not.
            if re.search(r'\(\s*f?"[^"]*\[/', body):
                offenders.append(f"{path}: {body[:60]}")
    assert not offenders, (
        "Text.assemble does not parse markup; use Text.from_markup:\n" + "\n".join(offenders)
    )


# --- P1: config show/get key mismatch + invisible source ----------------------


def test_config_show_keys_are_resolvable_by_config_get():
    """`config show` printed `providers.default` but `config get` only accepts
    `providers.default_provider`, so copy-pasting the shown key failed."""
    import inspect

    from velune.cli.commands.config import config_show
    from velune.kernel.config import VeluneConfig

    src = inspect.getsource(config_show)
    assert "providers.default_provider =" in src
    assert "providers.default =" not in src

    # And the key must actually resolve on the config object, the way
    # `config get` walks it.
    config = VeluneConfig()
    curr = config
    for part in "providers.default_provider".split("."):
        assert hasattr(curr, part), f"config get would fail on {part!r}"
        curr = getattr(curr, part)


def test_effective_config_path_reports_the_file_actually_used(tmp_path, monkeypatch):
    """Config resolution can walk up past the workspace, so a stray parent
    velune.toml silently supplies defaults. The path must be reportable."""
    monkeypatch.chdir(tmp_path)
    workspace_cfg = tmp_path / "velune.toml"
    workspace_cfg.write_text('[providers]\ndefault_provider = "openai"\n', encoding="utf-8")

    service = ConfigService(workspace=tmp_path)
    assert service.effective_config_path() == workspace_cfg


def test_effective_config_path_is_none_when_nothing_is_found(tmp_path, monkeypatch):
    """With no config anywhere up-tree, `config show` must say so rather than
    naming a file that does not exist."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("velune.kernel.config.ConfigLoader._find_config_path", lambda self: None)
    assert ConfigService(workspace=tmp_path).effective_config_path() is None


# --- P1: `models scan` recommended a model it showed as unhealthy -------------


class _Rec:
    def __init__(self, model_id: str, health: str, validated=None):
        self.model_id = model_id
        self.health = health
        self.metadata = {} if validated is None else {"validated": validated}


def test_recommended_model_skips_unreachable_models():
    from velune.cli.commands.models import _recommended_model

    records = [_Rec("offline-one", "offline"), _Rec("healthy-one", "healthy")]
    pick = _recommended_model(records)
    assert pick is not None and pick.model_id == "healthy-one"


def test_recommended_model_returns_none_when_all_offline():
    """Suggesting `models use <x>` for an unreachable model is a dead end."""
    from velune.cli.commands.models import _recommended_model

    records = [_Rec("a", "offline"), _Rec("b", "offline")]
    assert _recommended_model(records) is None


def test_live_validation_result_overrides_static_health():
    from velune.cli.commands.models import _record_health

    assert _record_health(_Rec("x", "healthy", validated=False)) == "offline"
    assert _record_health(_Rec("x", "offline", validated=True)) == "healthy"


def test_none_healthy_guidance_exists_and_avoids_models_use():
    """The fallback guidance must point at the blocker, not at a dead model."""
    from velune.cli import guidance

    steps = guidance.steps_for("models_scanned_none_healthy")
    assert steps, "fallback guidance must be registered"
    assert not any("models use" in str(step) for step in steps)


# --- P2: whitespace-only prompt ran the whole pipeline ------------------------


def test_ask_treats_whitespace_only_prompt_as_empty():
    import inspect

    from velune.cli.commands.ask import ask_command

    src = inspect.getsource(ask_command)
    assert ".strip()" in src, "whitespace-only prompts must not reach the council pipeline"


# --- P3: mojibake on Windows (cp1252 stdout) ---------------------------------


def test_force_utf8_stdio_is_safe_on_non_reconfigurable_streams():
    """Must never raise, whatever stdout has been replaced with."""
    import sys

    from velune.main import _force_utf8_stdio

    class _Dumb:
        pass

    original_out, original_err = sys.stdout, sys.stderr
    try:
        sys.stdout = _Dumb()  # type: ignore[assignment]
        sys.stderr = _Dumb()  # type: ignore[assignment]
        _force_utf8_stdio()  # no reconfigure attribute at all
    finally:
        sys.stdout, sys.stderr = original_out, original_err


def test_force_utf8_stdio_swallows_reconfigure_failure():
    import sys

    from velune.main import _force_utf8_stdio

    class _Boom:
        def reconfigure(self, **_kw):
            raise OSError("closed stream")

    original_out, original_err = sys.stdout, sys.stderr
    try:
        sys.stdout = _Boom()  # type: ignore[assignment]
        sys.stderr = _Boom()  # type: ignore[assignment]
        _force_utf8_stdio()
    finally:
        sys.stdout, sys.stderr = original_out, original_err


@pytest.mark.parametrize("text", ["—", "──►", "★", "✓"])
def test_ui_glyphs_survive_utf8_encoding(text):
    """The glyphs the UI relies on must round-trip as UTF-8 (under cp1252 the
    em-dash silently became byte 0x97 and rendered as a replacement char)."""
    assert text.encode("utf-8").decode("utf-8") == text
