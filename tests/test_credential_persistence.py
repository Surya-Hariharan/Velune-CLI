"""A saved API key survives restarts, failed writes and corruption — and never leaks.

Every test uses a temp config dir and a fixed fake master key; the real OS
keyring and the user's credential store are never touched.
"""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import patch

import pytest

from velune.providers import crypto
from velune.providers import keystore as ks
from velune.providers.keystore import CredentialManager, CredentialStoreError

FAKE_MASTER = base64.b64decode("QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE=")
KEY_A = "sk-test-aaaaaaaaaaaaaaaaaaaaaaaa"
KEY_B = "sk-ant-test-bbbbbbbbbbbbbbbbbbbbbbbb"


@pytest.fixture
def store(tmp_path, monkeypatch):
    for name in [*ks.all_provider_env_vars(), ks.PREFER_ENV_VAR]:
        monkeypatch.delenv(name, raising=False)
    with (
        patch("velune.providers.keystore.user_config_dir", return_value=str(tmp_path)),
        patch("velune.providers.crypto.get_or_create_master_key", return_value=FAKE_MASTER),
    ):
        CredentialManager._instance = None
        ks._manager._init()
        yield tmp_path
        CredentialManager._instance = None
        ks._manager._init()


def _restart() -> None:
    """A new process: fresh singleton state, same files on disk."""
    CredentialManager._instance = None
    ks._manager._init()


def _cred(tmp: Path) -> Path:
    return tmp / "credentials.json"


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------


def test_saved_key_survives_restart_with_its_state(store):
    ks.save_key("openai", KEY_A, verified=True)
    ks.save_key("anthropic", KEY_B)

    _restart()

    assert ks.get_key("openai") == KEY_A
    assert ks.get_key("anthropic") == KEY_B
    assert ks.verification_state("openai") is ks.KeyState.VERIFIED
    assert ks.verification_state("anthropic") is ks.KeyState.UNVERIFIED


def test_updating_one_provider_leaves_every_other_record_identical(store):
    ks.save_key("openai", KEY_A, verified=True)
    ks.save_key("anthropic", KEY_B, verified=True)
    before = dict(ks._manager.get_all_providers()["anthropic"])

    ks.save_key("openai", KEY_A + "x")
    _restart()

    assert ks._manager.get_all_providers()["anthropic"] == before
    assert ks.get_key("openai") == KEY_A + "x"


def test_a_key_written_by_another_process_is_not_reverted(store):
    """Process 1 has a cached view; process 2 adds a key; process 1 saves another."""
    ks.save_key("openai", KEY_A)
    ks._manager.get_all_providers()  # warm process 1's cache

    other = CredentialManager.__new__(CredentialManager)
    other._init()
    other.save_provider("anthropic", KEY_B)

    ks.save_key("groq", "gsk_" + "g" * 30)
    _restart()
    assert ks.get_key("anthropic") == KEY_B
    assert ks.get_key("openai") == KEY_A


# ---------------------------------------------------------------------------
# Failed and unconfirmed writes
# ---------------------------------------------------------------------------


def test_failed_write_reports_failure_and_keeps_the_old_key(store):
    ks.save_key("openai", KEY_A)

    def crash(self, target):
        raise OSError("disk full")

    with patch("pathlib.Path.replace", autospec=True, side_effect=crash):
        with pytest.raises(CredentialStoreError):
            ks.save_key("openai", "sk-new-cccccccccccccccccccccccc")

    # Not even this process believes the new key was saved.
    assert ks.get_key("openai") == KEY_A
    _restart()
    assert ks.get_key("openai") == KEY_A


def test_read_back_mismatch_is_not_reported_as_success(store):
    original = CredentialManager._decode

    calls = {"n": 0}

    def flaky_decode(path):
        calls["n"] += 1
        data = original(path)
        if calls["n"] >= 2:  # the read-back after the write
            data = {}
        return data

    ks.save_key("openai", KEY_A)
    with patch.object(CredentialManager, "_decode", staticmethod(flaky_decode)):
        with pytest.raises(CredentialStoreError, match="did not match"):
            ks.save_key("anthropic", KEY_B)


# ---------------------------------------------------------------------------
# Corruption and unreadable stores
# ---------------------------------------------------------------------------


def test_corrupt_primary_reads_from_backup_and_is_kept_for_recovery(store):
    ks.save_key("openai", KEY_A)
    ks.save_key("anthropic", KEY_B)  # creates credentials.json.bak with openai
    _cred(store).write_bytes(b"\x00garbage")
    _restart()

    assert ks.get_key("openai") == KEY_A  # read from the backup, in place

    ks.save_key("groq", "gsk_" + "g" * 30)
    kept = list(store.glob("credentials.json.unreadable-*"))
    assert kept and kept[0].read_bytes() == b"\x00garbage"
    _restart()
    assert ks.get_key("openai") == KEY_A
    assert ks.get_key("groq")


def test_an_unreadable_store_is_never_overwritten(store, monkeypatch):
    ks.save_key("openai", KEY_A)
    raw = _cred(store).read_bytes()
    (store / "credentials.json.bak").unlink(missing_ok=True)

    # Simulate the master key changing (keyring reset, different machine).
    _restart()
    with patch("velune.providers.crypto.get_or_create_master_key", return_value=b"B" * 32):
        assert ks.get_key("openai") is None
        assert ks.store_load_error()
        # Environment variables still work while the store is unreadable.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env-dddddddddddddddddddddddd")
        assert ks.get_key("openai") == "sk-env-dddddddddddddddddddddddd"
        with pytest.raises(CredentialStoreError, match="Nothing was overwritten"):
            ks.save_key("anthropic", KEY_B)

    assert _cred(store).read_bytes() == raw, "the unreadable store was modified"
    monkeypatch.delenv("OPENAI_API_KEY")
    _restart()
    assert ks.get_key("openai") == KEY_A  # the original key comes back with the right master key


def test_repair_moves_an_unreadable_store_aside_instead_of_deleting(store):
    ks.save_key("openai", KEY_A)
    (store / "credentials.json.bak").unlink(missing_ok=True)
    _restart()
    with patch("velune.providers.crypto.get_or_create_master_key", return_value=b"B" * 32):
        report = ks.repair_keystore()
        assert report["quarantined"]
        assert Path(report["quarantined"]).exists()
        ks.save_key("anthropic", KEY_B)  # a fresh store can be written now
        assert ks.get_key("anthropic") == KEY_B


def test_transient_keyring_failure_never_mints_a_new_master_key(monkeypatch):
    """A failed keyring *read* used to create (and store) a new master key,
    orphaning every credential encrypted with the old one."""
    created = []

    class FlakyKeyring:
        def get_password(self, *_a):
            raise RuntimeError("keychain locked")

        def set_password(self, *a):
            created.append(a)

    import sys
    import types

    fake = types.ModuleType("keyring")
    fake.get_password = FlakyKeyring().get_password
    fake.set_password = FlakyKeyring().set_password
    monkeypatch.setitem(sys.modules, "keyring", fake)
    monkeypatch.setenv("VELUNE_MASTER_PASSPHRASE", "test-passphrase")

    crypto.get_or_create_master_key()
    assert created == []


def test_keyring_with_no_entry_still_creates_one(monkeypatch):
    created = []
    import sys
    import types

    fake = types.ModuleType("keyring")
    fake.get_password = lambda *_a: None
    fake.set_password = lambda *a: created.append(a)
    monkeypatch.setitem(sys.modules, "keyring", fake)

    key = crypto.get_or_create_master_key()
    assert created and len(key) == 32


# ---------------------------------------------------------------------------
# Environment-variable precedence
# ---------------------------------------------------------------------------


def test_saved_key_wins_over_environment_by_default(store, monkeypatch):
    ks.save_key("openai", KEY_A, verified=True)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stale-env-eeeeeeeeeeeeeeeeeeee")

    assert ks.get_key("openai") == KEY_A
    src = ks.key_source("openai")
    assert src.source == "stored" and src.shadowed_env_var == "OPENAI_API_KEY"
    assert ks.verification_state("openai") is ks.KeyState.VERIFIED
    assert "OPENAI_API_KEY" in (ks.override_note("openai") or "")


def test_prefer_env_override_is_explicit_and_reported(store, monkeypatch):
    ks.save_key("openai", KEY_A)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-ffffffffffffffffffffffff")
    monkeypatch.setenv(ks.PREFER_ENV_VAR, "1")

    assert ks.get_key("openai") == "sk-env-ffffffffffffffffffffffff"
    assert ks.verification_state("openai") is ks.KeyState.ENV
    assert ks.key_source("openai").describe() == "$OPENAI_API_KEY (overrides the saved key)"


def test_environment_used_when_nothing_is_saved_and_whitespace_is_stripped(store, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "  sk-env-gggggggggggggggggggggggg\n")
    assert ks.get_key("openai") == "sk-env-gggggggggggggggggggggggg"
    assert ks.verification_state("openai") is ks.KeyState.ENV


def test_ngc_api_key_is_accepted_for_nvidia(store, monkeypatch):
    monkeypatch.setenv("NGC_API_KEY", "nvapi-ngc-hhhhhhhhhhhhhhhhhhhh")
    assert ks.get_key("nvidia") == "nvapi-ngc-hhhhhhhhhhhhhhhhhhhh"
    assert ks.key_source("nvidia").env_var == "NGC_API_KEY"


async def test_reverify_of_an_env_key_never_rewrites_the_saved_record(store, monkeypatch):
    from velune.providers import verifier
    from velune.providers.validation import ValidationResult, ValidationStatus

    ks.save_key("openai", KEY_A, verified=True)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-iiiiiiiiiiiiiiiiiiiiiiii")
    monkeypatch.setenv(ks.PREFER_ENV_VAR, "1")

    async def reject(pid, key=""):
        return ValidationResult(pid, ValidationStatus.INVALID_KEY, "nope")

    monkeypatch.setattr(verifier, "validate_provider", reject)
    await verifier.reverify("openai")
    monkeypatch.delenv(ks.PREFER_ENV_VAR)
    assert ks.verification_state("openai") is ks.KeyState.VERIFIED


# ---------------------------------------------------------------------------
# Secrets never leak
# ---------------------------------------------------------------------------


def test_secrets_never_appear_in_files_status_or_messages(store, monkeypatch):
    from velune.providers.credential_manager import CredentialAddResult
    from velune.providers.validation import ValidationResult, ValidationStatus

    ks.save_key("openai", KEY_A, verified=True)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-jjjjjjjjjjjjjjjjjjjjjjjj")

    assert KEY_A.encode() not in _cred(store).read_bytes()
    surfaces = [
        repr(ks.get_provider_status("openai")),
        ks.key_source("openai").describe(),
        ks.override_note("openai") or "",
        CredentialAddResult(
            ok=False,
            provider_id="openai",
            saved=True,
            verified=False,
            became_default=False,
            validation=ValidationResult("openai", ValidationStatus.TIMEOUT, "timed out"),
        ).summary(),
    ]
    for text in surfaces:
        assert KEY_A not in text and "sk-env-jjjj" not in text
