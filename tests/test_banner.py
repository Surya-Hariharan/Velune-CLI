import io
from unittest.mock import MagicMock

from rich.console import Console

from velune.cli.banner import render_startup_banner
from velune.hardware.detector import HardwareProfile, HardwareTier


def _mock_profile(tier: HardwareTier = HardwareTier.CAPABLE) -> HardwareProfile:
    return HardwareProfile(
        total_ram_gb=16.0,
        available_ram_gb=10.0,
        gpu_name="NVIDIA RTX 3060",
        vram_total_gb=8.0,
        is_apple_silicon=False,
        cpu_cores=8,
        platform="linux",
        tier=tier,
        recommended_model_size="7B",
        can_run_local_llm=True,
        warnings=[],
        suggestions=[],
    )


def _captured_output(**kwargs) -> str:
    """Render banner to a string buffer and return the plain text."""
    buf = io.StringIO()
    console = Console(file=buf, highlight=False, markup=True)
    render_startup_banner(console=console, **kwargs)
    return buf.getvalue()


def test_banner_renders_without_error():
    console = MagicMock()
    render_startup_banner(
        console=console,
        hardware_profile=_mock_profile(),
        configured_providers=["groq"],
        ollama_live=True,
        workspace_name="my-project",
        active_model_id="llama3:8b",
        version="0.1.0",
    )
    console.print.assert_called()


def test_banner_shows_warning_on_critical_hardware():
    profile = _mock_profile(tier=HardwareTier.CRITICAL)
    profile.warnings = ["Only 6 GB RAM — cannot run any local LLM"]
    profile.suggestions = ["Use Groq free tier"]
    console = MagicMock()
    render_startup_banner(
        console=console,
        hardware_profile=profile,
        configured_providers=[],
        ollama_live=False,
        workspace_name="test",
        active_model_id=None,
        version="0.1.0",
    )
    calls = " ".join(str(c) for c in console.print.call_args_list)
    assert "RAM" in calls or "warning" in calls.lower() or "⚠" in calls


def test_banner_no_providers_shows_fallback():
    out = _captured_output(
        hardware_profile=_mock_profile(),
        configured_providers=[],
        ollama_live=False,
        workspace_name="test",
        active_model_id=None,
        version="0.1.0",
    )
    assert "no providers" in out


def test_banner_no_model_shows_prompt():
    out = _captured_output(
        hardware_profile=_mock_profile(),
        configured_providers=["openai"],
        ollama_live=False,
        workspace_name="test",
        active_model_id=None,
        version="0.1.0",
    )
    assert "/model" in out


def test_banner_ollama_live_shows_in_providers():
    out = _captured_output(
        hardware_profile=_mock_profile(),
        configured_providers=[],
        ollama_live=True,
        workspace_name="test",
        active_model_id=None,
        version="0.1.0",
    )
    assert "ollama" in out


def test_banner_shows_version():
    out = _captured_output(
        hardware_profile=_mock_profile(),
        configured_providers=[],
        ollama_live=False,
        workspace_name="myws",
        active_model_id=None,
        version="1.2.3",
    )
    assert "1.2.3" in out


def test_banner_shows_workspace_name():
    out = _captured_output(
        hardware_profile=_mock_profile(),
        configured_providers=[],
        ollama_live=False,
        workspace_name="velune-project",
        active_model_id=None,
        version="0.1.0",
    )
    assert "velune-project" in out
