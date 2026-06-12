"""Week 1 integration test suite — covers all six shipped features.

Features covered:
  1. Hardware Detection  (velune/hardware/detector.py)
  2. Keystore           (velune/providers/keystore.py)
  3. Groq Provider      (velune/providers/adapters/groq.py)
  4. Diff Preview       (velune/execution/diff_preview.py)
  5. Token Tracker      (velune/telemetry/token_tracker.py)
  6. Cancellation Guard (velune/execution/cancellation.py)
"""

from unittest.mock import MagicMock, patch

import pytest

# ── Hardware Detection ─────────────────────────────────────────────────────────


def test_hardware_profile_has_all_fields():
    from velune.hardware.detector import HardwareDetector

    profile = HardwareDetector().detect()
    assert profile.total_ram_gb > 0
    assert profile.cpu_cores > 0
    assert profile.platform in ("linux", "darwin", "windows")
    assert profile.tier is not None
    assert isinstance(profile.can_run_local_llm, bool)
    assert isinstance(profile.warnings, list)
    assert isinstance(profile.suggestions, list)


def test_hardware_tier_critical_below_8gb():
    from velune.hardware.detector import HardwareDetector, HardwareTier

    d = HardwareDetector()
    tier, rec, can_run = d._classify(6.0, None, False)
    assert tier == HardwareTier.CRITICAL
    assert can_run is False


def test_hardware_tier_elite_apple_36gb():
    from velune.hardware.detector import HardwareDetector, HardwareTier

    d = HardwareDetector()
    tier, rec, can_run = d._classify(36.0, 36.0, True)
    assert tier == HardwareTier.ELITE
    assert rec == "70B"


def test_hardware_tier_capable_16gb_gpu():
    from velune.hardware.detector import HardwareDetector, HardwareTier

    d = HardwareDetector()
    tier, rec, can_run = d._classify(16.0, 8.0, False)
    assert tier == HardwareTier.CAPABLE
    assert can_run is True


def test_hardware_marginal_16gb_no_gpu():
    from velune.hardware.detector import HardwareDetector, HardwareTier

    d = HardwareDetector()
    tier, rec, can_run = d._classify(16.0, None, False)
    assert tier == HardwareTier.MARGINAL


# ── Keystore ───────────────────────────────────────────────────────────────────


def test_keystore_env_fallback(monkeypatch):
    from velune.providers import keystore

    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key-123")
    with patch("keyring.get_password", return_value=None):
        key = keystore.get_key("groq")
    assert key == "test-groq-key-123"


def test_keystore_has_key_false_without_key(monkeypatch):
    from velune.providers import keystore

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with patch("keyring.get_password", return_value=None):
        result = keystore.has_key("groq")
    assert result is False


def test_keystore_save_and_retrieve():
    from velune.providers import keystore

    # Service is "velune/{provider_id}", username is "api_key" — per keystore._SERVICE / _USERNAME
    with (
        patch("keyring.set_password") as mock_set,
        patch("keyring.get_password", return_value="sk-test-key"),
    ):
        keystore.save_key("openai", "sk-test-key")
        mock_set.assert_called_once_with("velune/openai", "api_key", "sk-test-key")
        key = keystore.get_key("openai")
    assert key == "sk-test-key"


# ── Groq Provider ──────────────────────────────────────────────────────────────


def test_groq_models_not_empty():
    from velune.providers.adapters.groq import GROQ_MODELS

    assert len(GROQ_MODELS) >= 4


def test_groq_all_free_tier():
    from velune.providers.adapters.groq import GROQ_MODELS

    for m in GROQ_MODELS:
        assert m.free_tier is True
        assert m.cost_per_1k_tokens == 0.0


def test_groq_context_lengths():
    from velune.providers.adapters.groq import GROQ_MODELS

    flagship = next(m for m in GROQ_MODELS if "70b" in m.model_id)
    assert flagship.context_length >= 32768


@pytest.mark.asyncio
async def test_groq_discovery_empty_without_key(monkeypatch):
    import velune.providers.keystore as ks

    monkeypatch.setattr(ks, "has_key", lambda x: False)
    from velune.providers.discovery.groq import GroqDiscovery

    result = await GroqDiscovery().discover()
    assert result == []


# ── Diff Preview ───────────────────────────────────────────────────────────────


def test_diff_detects_new_file(tmp_path):
    from velune.execution.diff_preview import DiffPreview

    console = MagicMock()
    p = DiffPreview(console)
    diff = p.compute_diff(tmp_path / "new.py", "x = 1")
    assert diff.is_new_file is True


def test_diff_detects_modification(tmp_path):
    from velune.execution.diff_preview import DiffPreview

    console = MagicMock()
    p = DiffPreview(console)
    f = tmp_path / "mod.py"
    f.write_text("x = 1")
    diff = p.compute_diff(f, "x = 2")
    assert diff.is_new_file is False
    assert diff.original == "x = 1"


@pytest.mark.asyncio
async def test_diff_auto_accept(tmp_path):
    from velune.execution.diff_preview import DiffDecision, DiffPreview

    console = MagicMock()
    p = DiffPreview(console)
    decision = await p.preview_and_confirm(tmp_path / "auto.py", "x = 1", auto_accept=True)
    assert decision == DiffDecision.ACCEPT


@pytest.mark.asyncio
async def test_diff_reject_preserves_file(tmp_path):
    from velune.execution.diff_preview import DiffDecision, DiffPreview

    console = MagicMock()
    p = DiffPreview(console)
    f = tmp_path / "orig.py"
    f.write_text("original")
    with patch("rich.prompt.Prompt.ask", return_value="r"):
        decision = await p.preview_and_confirm(f, "changed")
    assert decision == DiffDecision.REJECT
    assert f.read_text() == "original"


# ── Token Tracker ──────────────────────────────────────────────────────────────


def test_token_cost_zero_for_groq():
    from velune.telemetry.token_tracker import TokenUsage

    u = TokenUsage.from_response("groq", "llama-3.3-70b-versatile", 1000, 500)
    assert u.cost_usd == 0.0
    assert u.total_tokens == 1500


def test_token_cost_nonzero_for_anthropic():
    from velune.telemetry.token_tracker import TokenUsage

    u = TokenUsage.from_response("anthropic", "claude-haiku-4-5", 1000, 500)
    assert u.cost_usd > 0


def test_session_usage_accumulation():
    from velune.telemetry.token_tracker import SessionUsage, TokenUsage

    s = SessionUsage()
    s.add(TokenUsage.from_response("groq", "llama-3.3-70b-versatile", 100, 50))
    s.add(TokenUsage.from_response("groq", "llama-3.3-70b-versatile", 200, 100))
    assert s.total_tokens == 450
    assert s.prompt_tokens == 300
    assert s.completion_tokens == 150


def test_session_summary_shows_free():
    from velune.telemetry.token_tracker import SessionUsage, TokenUsage

    s = SessionUsage()
    s.add(TokenUsage.from_response("groq", "llama-3.3-70b-versatile", 500, 250))
    line = s.summary_line()
    assert "free" in line
    assert "750" in line


# ── Cancellation ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancellation_token_default_not_cancelled():
    from velune.execution.cancellation import CancellationToken

    t = CancellationToken()
    assert not t.is_cancelled


@pytest.mark.asyncio
async def test_guard_survives_keyboard_interrupt():
    from velune.execution.cancellation import InferenceGuard

    console = MagicMock()
    guard = InferenceGuard(console)
    raised = False
    try:
        async with guard.guard():
            raise KeyboardInterrupt()
    except Exception:
        raised = True
    assert not raised, "Guard should suppress KeyboardInterrupt"


@pytest.mark.asyncio
async def test_guard_clears_token_on_exit():
    from velune.execution.cancellation import InferenceGuard

    console = MagicMock()
    guard = InferenceGuard(console)
    async with guard.guard():
        pass
    assert guard._current_token is None
