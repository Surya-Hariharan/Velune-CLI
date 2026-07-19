"""Provider credential lifecycle: verified / unverified / stale / invalid.

Before this, ``save_provider`` hardcoded ``status="valid"`` on *every* save —
including the two paths that deliberately skip validation ("Save anyway" and
``--no-validate``) — so a never-checked key was indistinguishable on disk from
one the provider had accepted. Nothing ever read the status back, and nothing
ever re-verified. These tests pin down the behaviour that replaced it.
"""

from __future__ import annotations

import datetime as dt

import pytest

from velune.providers import keystore as ks
from velune.providers import verifier
from velune.providers.keystore import KeyState
from velune.providers.validation import ValidationResult, ValidationStatus


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Point the credential singleton at a temp file and clear the env."""
    for env_var in ks.PROVIDER_ENV_VARS.values():
        monkeypatch.delenv(env_var, raising=False)

    monkeypatch.setattr(ks._manager, "_config_dir", tmp_path)
    monkeypatch.setattr(ks._manager, "_credentials_file", tmp_path / "credentials.json")
    monkeypatch.setattr(ks._manager, "_cache", None)
    yield


def _backdate(pid: str, *, seconds: float) -> None:
    """Move a provider's last_verified stamp into the past."""
    stamp = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)
    ks._manager._cache[pid]["last_verified"] = stamp.isoformat()


# ---------------------------------------------------------------------------
# verification_state
# ---------------------------------------------------------------------------


def test_missing_when_no_key_stored():
    assert ks.verification_state("openai") is KeyState.MISSING


def test_save_without_verified_flag_is_unverified():
    """The regression that motivated all of this.

    A caller that skipped validation must not be able to record a key as
    verified by omission.
    """
    ks.save_key("openai", "sk-never-checked")
    assert ks.verification_state("openai") is KeyState.UNVERIFIED


def test_save_with_verified_flag_is_verified():
    ks.save_key("openai", "sk-good", verified=True)
    assert ks.verification_state("openai") is KeyState.VERIFIED


def test_rejected_key_is_invalid():
    ks.save_key("openai", "sk-good", verified=True)
    ks.mark_invalid("openai", reason="Invalid OpenAI API key.")
    assert ks.verification_state("openai") is KeyState.INVALID
    assert ks.list_invalid_providers() == ["openai"]


def test_verified_key_goes_stale_past_the_ttl():
    ks.save_key("openai", "sk-good", verified=True)
    _backdate("openai", seconds=ks.VERIFY_TTL_SECONDS + 60)

    assert ks.verification_state("openai") is KeyState.STALE
    assert ks.list_stale_providers() == ["openai"]


def test_key_just_inside_the_ttl_is_still_verified():
    ks.save_key("openai", "sk-good", verified=True)
    _backdate("openai", seconds=ks.VERIFY_TTL_SECONDS - 60)

    assert ks.verification_state("openai") is KeyState.VERIFIED


def test_stale_key_is_still_returned():
    """Stale means "re-check soon", not "unusable" — refusing to hand it back
    would break every caller for a key that is probably fine."""
    ks.save_key("openai", "sk-good", verified=True)
    _backdate("openai", seconds=ks.VERIFY_TTL_SECONDS + 60)

    assert ks.get_key("openai") == "sk-good"


def test_env_sourced_key_reports_env(monkeypatch):
    """We don't own the lifecycle of a key we didn't store."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    assert ks.verification_state("openai") is KeyState.ENV


def test_legacy_valid_status_still_loads():
    """A credentials.json written before the lifecycle existed used status="valid".

    Those records are mapped on read rather than migrated, so an existing install
    keeps working: a recently-stamped "valid" record reads as VERIFIED.
    """
    ks._manager.save_provider("openai", "sk-old", status="valid")
    assert ks.verification_state("openai") is KeyState.VERIFIED


def test_legacy_valid_status_respects_the_ttl():
    """...and an *old* legacy record still goes stale rather than being trusted
    forever, which is precisely the bug this replaced."""
    ks._manager.save_provider("openai", "sk-old", status="valid")
    _backdate("openai", seconds=ks.VERIFY_TTL_SECONDS + 60)

    assert ks.verification_state("openai") is KeyState.STALE


def test_legacy_imported_status_is_unverified():
    """A key restored from a backup was never checked *by us*."""
    ks._manager.save_provider("openai", "sk-restored", status="imported")
    assert ks.verification_state("openai") is KeyState.UNVERIFIED


# ---------------------------------------------------------------------------
# delete_provider — a deletion must actually persist, not just vanish from
# the in-memory cache for the lifetime of one process.
#
# _save_disk() merges its argument onto a *fresh* read of the on-disk state
# (so a concurrent save for a different provider isn't clobbered by a stale
# in-memory snapshot) via plain dict.update(). update() can only add/overwrite
# keys — a provider merely absent from the merge argument is indistinguishable
# from one nobody touched, so the fresh disk read silently re-introduced the
# very record delete_provider() had just removed from the cache. Confirmed
# live: `velune provider add nvidia <bad-key>` (accepted — NVIDIA's /v1/models
# doesn't reject it), then deleting it and reloading fresh from disk showed it
# right back, with the exact original timestamp, proving nothing was ever
# written for the deletion.
# ---------------------------------------------------------------------------


def test_delete_provider_persists_across_a_fresh_disk_read():
    ks.save_key("openai", "sk-to-be-deleted", verified=True)
    ks.delete_key("openai")

    # Force a real disk re-read — the bug only manifested here, since the
    # in-memory cache correctly dropped the key; the file did not.
    ks._manager._cache = None
    assert "openai" not in ks._manager._load_disk()
    assert ks.verification_state("openai") is KeyState.MISSING


def test_delete_provider_does_not_resurrect_via_a_later_save_of_another_key():
    """The merge-with-fresh-disk-read behavior must still protect concurrent
    saves for *other* providers — only the deleted id should be gone."""
    ks.save_key("openai", "sk-openai", verified=True)
    ks.save_key("anthropic", "sk-anthropic", verified=True)

    ks.delete_key("openai")
    ks.save_key("groq", "sk-groq", verified=True)  # an unrelated concurrent-ish save

    ks._manager._cache = None
    on_disk = ks._manager._load_disk()
    assert "openai" not in on_disk
    assert on_disk["anthropic"]["key"] == "sk-anthropic"
    assert on_disk["groq"]["key"] == "sk-groq"


# ---------------------------------------------------------------------------
# verifier — the rule that keeps offline users out of trouble
# ---------------------------------------------------------------------------


def _stub_validation(monkeypatch, status: ValidationStatus, models=()):
    async def _fake(provider_id: str, api_key: str = ""):
        return ValidationResult(provider_id, status, "stubbed", models=list(models))

    monkeypatch.setattr(verifier, "validate_provider", _fake)


@pytest.mark.parametrize(
    "status",
    [ValidationStatus.NETWORK_ERROR, ValidationStatus.RATE_LIMITED],
)
async def test_inconclusive_verdict_leaves_the_key_stale(monkeypatch, status):
    """An offline user must never be told their key is broken.

    A network error or a rate limit says something about the network, not about
    the credential — so the record stays STALE and is retried later.
    """
    ks.save_key("openai", "sk-good", verified=True)
    _backdate("openai", seconds=ks.VERIFY_TTL_SECONDS + 60)
    _stub_validation(monkeypatch, status)

    await verifier.reverify_stale()

    assert ks.verification_state("openai") is KeyState.STALE


@pytest.mark.parametrize(
    "status",
    [
        ValidationStatus.INVALID_KEY,
        ValidationStatus.EXPIRED_KEY,
        ValidationStatus.REVOKED_KEY,
        ValidationStatus.PERMISSION_DENIED,
    ],
)
async def test_key_rejecting_verdict_marks_invalid(monkeypatch, status):
    ks.save_key("openai", "sk-bad", verified=True)
    _backdate("openai", seconds=ks.VERIFY_TTL_SECONDS + 60)
    _stub_validation(monkeypatch, status)

    await verifier.reverify_stale()

    assert ks.verification_state("openai") is KeyState.INVALID


async def test_successful_reverify_restores_verified(monkeypatch):
    ks.save_key("openai", "sk-good", verified=True)
    _backdate("openai", seconds=ks.VERIFY_TTL_SECONDS + 60)
    _stub_validation(monkeypatch, ValidationStatus.OK, models=["a", "b"])

    await verifier.reverify_stale()

    assert ks.verification_state("openai") is KeyState.VERIFIED


async def test_reverify_stale_ignores_fresh_and_unverified_keys(monkeypatch):
    """Only STALE records are swept — a fresh key needs no round-trip, and an
    UNVERIFIED one is the user's explicit "save it anyway" choice."""
    ks.save_key("openai", "sk-fresh", verified=True)
    ks.save_key("groq", "gsk-unverified")

    called: list[str] = []

    async def _fake(provider_id: str, api_key: str = ""):
        called.append(provider_id)
        return ValidationResult(provider_id, ValidationStatus.OK, "ok")

    monkeypatch.setattr(verifier, "validate_provider", _fake)

    await verifier.reverify_stale()

    assert called == []
