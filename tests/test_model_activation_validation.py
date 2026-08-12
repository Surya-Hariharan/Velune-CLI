"""`/model use` (and every other path into activate_model) must not silently
declare a model active when its provider has no usable credential.

Regression coverage: previously `activate_model()` unconditionally set
`repl.active_model` and printed "Active model: ..." for any model found in
the local registry, regardless of whether the provider was ever connected —
so a stale or removed credential only surfaced later, mid-turn, as a raw
provider error instead of at the point of selection.

Deliberately narrow: only MISSING and INVALID credential states block
activation. STALE/UNVERIFIED/VERIFIED/ENV are all still allowed — a
never-yet-checked or due-for-recheck key is not "broken", and blocking on
that would make normal ordinary use (a fresh `/connect`, a key that simply
hasn't been re-verified in 24h) fail for no reason.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from velune.cli.handlers.model import _activation_blocker, activate_model
from velune.providers.keystore import CredentialManager, save_key


@pytest.fixture
def mock_config_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("velune.providers.keystore.user_config_dir", return_value=str(tmp_path / "cfg")):
        CredentialManager._instance = None
        from velune.providers.keystore import _manager

        _manager._init()
        yield tmp_path
        CredentialManager._instance = None
        _manager._init()


@pytest.fixture
def mock_keyring():
    with patch("velune.providers.crypto.get_or_create_master_key") as mock_get_key:
        mock_get_key.return_value = base64.b64decode("QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE=")
        yield mock_get_key


def _model(model_id="llama-3.3-70b-versatile", provider_id="groq", context_length=131072):
    return SimpleNamespace(
        model_id=model_id,
        provider_id=provider_id,
        context_length=context_length,
        is_local=False,
    )


def _repl():
    printed = []
    return SimpleNamespace(
        console=SimpleNamespace(print=lambda *a, **k: printed.append(a)),
        active_model=None,
        _context_tracker=None,
    ), printed


# --- _activation_blocker: the pure decision function --------------------------


def test_missing_credential_blocks_activation(mock_config_dir, mock_keyring):
    assert _activation_blocker(_model()) is not None


def test_invalid_credential_blocks_activation(mock_config_dir, mock_keyring):
    from velune.providers.keystore import mark_invalid

    save_key("groq", "gsk_bad", verified=False)
    mark_invalid("groq", reason="401")

    blocker = _activation_blocker(_model())
    assert blocker is not None
    assert "groq" in blocker


def test_verified_credential_allows_activation(mock_config_dir, mock_keyring):
    save_key("groq", "gsk_good", verified=True)
    assert _activation_blocker(_model()) is None


def test_unverified_credential_allows_activation(mock_config_dir, mock_keyring):
    """Saved but never checked — e.g. --no-validate — must not block."""
    save_key("groq", "gsk_unverified", verified=False)
    assert _activation_blocker(_model()) is None


def test_stale_credential_allows_activation(mock_config_dir, mock_keyring):
    """Due for a re-check is not the same as broken."""
    import datetime

    save_key("groq", "gsk_old", verified=True)
    from velune.providers.keystore import _manager

    record = _manager.get_provider("groq")
    record = dict(record)
    record["last_verified"] = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)
    ).isoformat()
    _manager._cache["groq"] = record
    _manager._save_disk(_manager._cache)

    assert _activation_blocker(_model()) is None


def test_local_provider_never_needs_a_credential_check(mock_config_dir, mock_keyring):
    assert _activation_blocker(_model("llama3.2", "ollama")) is None


def test_env_sourced_credential_allows_activation(mock_config_dir, mock_keyring, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_from_env")
    assert _activation_blocker(_model()) is None


# --- activate_model: the actual gate ------------------------------------------


@pytest.mark.asyncio
async def test_activate_model_refuses_when_credential_is_missing(mock_config_dir, mock_keyring):
    repl, printed = _repl()
    await activate_model(repl, _model())

    assert repl.active_model is None
    assert any("Cannot activate" in str(a) for call in printed for a in call)


@pytest.mark.asyncio
async def test_activate_model_succeeds_when_credential_is_verified(
    mock_config_dir, mock_keyring, monkeypatch
):
    save_key("groq", "gsk_good", verified=True)
    monkeypatch.setattr("velune.cli.model_prefs.save_active_model", lambda *a, **k: None)
    monkeypatch.setattr("velune.cli.model_prefs.add_recent", lambda *a, **k: None)
    monkeypatch.setattr("velune.cli.handlers.model._persist_default_provider", lambda *a, **k: None)

    repl, printed = _repl()
    model = _model()
    await activate_model(repl, model)

    assert repl.active_model is model
    assert any("Active model" in str(a) for call in printed for a in call)
