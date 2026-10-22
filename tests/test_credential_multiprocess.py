"""The credential store under real concurrency and a flaky OS keyring.

Two groups of tests, both fully isolated (temp config dir, fake keyring, no
network, nothing under the real ``~/.velune`` or the real keyring):

* separate OS **processes** saving different providers at once, and a process
  that dies in the middle of a write;
* a keyring that is temporarily locked, and primary/backup files that are
  empty, truncated or corrupt.
"""

from __future__ import annotations

import base64
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from velune.providers import crypto
from velune.providers import keystore as ks
from velune.providers.keystore import CredentialManager, CredentialStoreError

REPO = Path(__file__).resolve().parent.parent
WORKER = Path(__file__).with_name("_cred_worker.py")
FAKE_MASTER = base64.b64decode("QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE=")
KEY_A = "sk-test-aaaaaaaaaaaaaaaaaaaaaaaa"
KEY_B = "sk-ant-test-bbbbbbbbbbbbbbbbbbbbbbbb"
KEY_C = "gsk-test-cccccccccccccccccccccccc"


def _spawn(cfg: Path, go: Path, prefix: str, count: int, *extra: str) -> subprocess.Popen:
    env = {"PYTHONPATH": str(REPO), "PYTHONIOENCODING": "utf-8"}
    import os

    env = {**os.environ, **env}
    return subprocess.Popen(
        [sys.executable, str(WORKER), str(cfg), str(go), prefix, str(count), *extra],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point the in-process manager at a temp dir with a fixed fake master key."""
    for name in [*ks.all_provider_env_vars(), ks.PREFER_ENV_VAR, "VELUNE_MASTER_PASSPHRASE"]:
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


# ---------------------------------------------------------------------------
# Separate processes
# ---------------------------------------------------------------------------


def test_concurrent_processes_saving_different_providers_all_survive(isolated):
    go = isolated / "go"
    per_worker = 8
    workers = [_spawn(isolated, go, p, per_worker) for p in ("a", "b", "c")]
    go.write_text("go")

    for proc in workers:
        out, err = proc.communicate(timeout=180)
        assert proc.returncode == 0, err

    ks._manager._init()
    stored = ks._manager.get_all_providers()
    expected = {f"{p}{i}" for p in ("a", "b", "c") for i in range(per_worker)}
    assert set(stored) == expected, f"lost updates: {sorted(expected - set(stored))}"
    for pid in expected:
        assert stored[pid]["key"] == f"sk-test-{pid[0]}-{pid[1:]}"


def test_a_process_killed_mid_write_leaves_the_store_intact_and_unlocked(isolated):
    ks.save_key("openai", KEY_A, verified=True)
    before = (isolated / "credentials.json").read_bytes()

    go = isolated / "go"
    go.write_text("go")
    doomed = _spawn(isolated, go, "x", 1, "crash")
    doomed.communicate(timeout=60)
    assert doomed.returncode == 9  # died after writing the temp file, before the rename

    # The committed store is exactly what it was, and the dead process's lock is gone.
    assert (isolated / "credentials.json").read_bytes() == before
    ks._manager._init()
    assert ks.get_key("openai") == KEY_A
    ks.save_key("anthropic", KEY_B, verified=True)  # would raise FileLockTimeout if stuck
    assert ks.get_key("anthropic") == KEY_B
    assert ks.get_key("openai") == KEY_A
    assert ks.get_key("x0") is None


def test_a_lock_held_too_long_is_reported_as_a_store_error_not_a_crash(isolated):
    from velune.core.filelock import FileLock
    from velune.core.filelock import locked as real_locked

    ks.save_key("openai", KEY_A, verified=True)
    holder = FileLock(isolated / "credentials.json")
    holder.acquire()
    try:
        with patch("velune.providers.keystore.locked", lambda path: real_locked(path, timeout=0.2)):
            with pytest.raises(CredentialStoreError, match="Another Velune process"):
                ks.save_key("anthropic", KEY_B)
    finally:
        holder.release()

    assert ks.get_key("openai") == KEY_A
    assert ks.get_key("anthropic") is None  # nothing half-written
    ks.save_key("anthropic", KEY_B, verified=True)  # and the store is usable again
    assert ks.get_key("anthropic") == KEY_B


# ---------------------------------------------------------------------------
# Keyring availability
# ---------------------------------------------------------------------------


class FakeKeyring:
    """A stand-in ``keyring`` module whose reads can be made to fail."""

    def __init__(self, value: str | None = None) -> None:
        self.value = value
        self.reads = 0
        self.writes = 0
        self.fail_reads = False
        self.fail_on_read_number: int | None = None

    def get_password(self, service: str, username: str) -> str | None:
        self.reads += 1
        if self.fail_reads or self.reads == self.fail_on_read_number:
            raise RuntimeError("keyring is locked")
        return self.value

    def set_password(self, service: str, username: str, value: str) -> None:
        self.writes += 1
        self.value = value

    def master_key(self) -> bytes:
        assert self.value is not None
        return base64.b64decode(self.value)


@pytest.fixture
def real_crypto(tmp_path, monkeypatch):
    """The real master-key logic, with a fake keyring and a temp config dir."""
    for name in [*ks.all_provider_env_vars(), ks.PREFER_ENV_VAR, "VELUNE_MASTER_PASSPHRASE"]:
        monkeypatch.delenv(name, raising=False)
    fake = FakeKeyring()
    monkeypatch.setitem(sys.modules, "keyring", fake)
    monkeypatch.setattr(crypto, "_warned_no_protection", False)
    with (
        patch("velune.providers.keystore.user_config_dir", return_value=str(tmp_path)),
        patch("velune.providers.crypto.user_config_dir", return_value=str(tmp_path)),
    ):
        CredentialManager._instance = None
        ks._manager._init()
        yield tmp_path, fake
        CredentialManager._instance = None
        ks._manager._init()


def test_a_locked_keyring_never_replaces_the_master_key_or_the_store(real_crypto):
    tmp, kr = real_crypto
    ks.save_key("openai", KEY_A, verified=True)
    master = kr.master_key()
    writes_before = kr.writes
    primary = (tmp / "credentials.json").read_bytes()

    kr.fail_reads = True
    ks._manager._init()  # a new process that starts while the keyring is locked
    assert ks.get_key("openai") is None  # reads degrade; the store is not "empty"
    assert ks.store_load_error()

    with pytest.raises(CredentialStoreError) as caught:
        ks.save_key("anthropic", KEY_B)
    message = str(caught.value)
    assert KEY_A not in message and KEY_B not in message
    assert base64.b64encode(master).decode() not in message

    assert kr.writes == writes_before  # no new master key was minted
    assert kr.master_key() == master
    assert (tmp / "credentials.json").read_bytes() == primary  # untouched


def test_store_becomes_readable_again_once_the_keyring_recovers(real_crypto):
    tmp, kr = real_crypto
    ks.save_key("openai", KEY_A, verified=True)

    kr.fail_reads = True
    ks._manager._init()
    assert ks.get_key("openai") is None  # keyring locked at startup

    kr.fail_reads = False  # user unlocks the keyring; the file did not change
    assert ks.get_key("openai") is None  # still inside the retry pause: no re-derivation storm
    ks._manager._load_failed_at = -1e9  # let the retry pause elapse
    assert ks.get_key("openai") == KEY_A

    ks.save_key("anthropic", KEY_B, verified=True)
    ks._manager._init()
    assert ks.get_key("openai") == KEY_A
    assert ks.get_key("anthropic") == KEY_B


def test_a_keyring_hiccup_between_reading_and_writing_does_not_downgrade_encryption(real_crypto):
    tmp, kr = real_crypto
    ks.save_key("openai", KEY_A, verified=True)
    master = kr.master_key()
    before = (tmp / "credentials.json").read_bytes()

    # Read #1 of a save decrypts the existing store; read #2 picks the write key.
    kr.fail_on_read_number = kr.reads + 2
    try:
        ks.save_key("anthropic", KEY_B, verified=True)
    except Exception:
        # Refusing the save is acceptable, but then nothing may have changed.
        assert (tmp / "credentials.json").read_bytes() == before
        return

    blob = (tmp / "credentials.json").read_bytes()
    # Saved: it must still be protected by the real master key, not the
    # machine-derived fallback that is only meant for keyring-less hosts.
    AESGCM(master).decrypt(blob[:12], blob[12:], None)


# ---------------------------------------------------------------------------
# Primary / backup damage
# ---------------------------------------------------------------------------


def _three_saves(tmp: Path) -> bytes:
    ks.save_key("openai", KEY_A, verified=True)
    ks.save_key("anthropic", KEY_B, verified=True)
    ks.save_key("groq", KEY_C, verified=True)
    return (tmp / "credentials.json.bak").read_bytes()


def test_an_empty_primary_falls_back_to_the_backup_and_keeps_it(isolated):
    _three_saves(isolated)  # the backup holds the first two; groq exists only in the primary
    (isolated / "credentials.json").write_bytes(b"")  # e.g. a crash or full disk
    ks._manager._init()

    assert ks.get_key("openai") == KEY_A  # not silently "no keys"
    assert ks.get_key("anthropic") == KEY_B

    ks.save_key("mistral", "mistral-test-dddddddddddddddddddd", verified=True)

    ks._manager._init()
    assert {ks.get_key(p) for p in ("openai", "anthropic")} == {KEY_A, KEY_B}
    assert ks.get_key("mistral") == "mistral-test-dddddddddddddddddddd"
    # The empty file did not replace the backup, and it was set aside, not lost.
    assert (isolated / "credentials.json.bak").stat().st_size > 0
    assert list(isolated.glob("credentials.json.unreadable-*"))


def test_an_empty_primary_with_no_backup_is_just_a_fresh_store(isolated):
    (isolated / "credentials.json").write_bytes(b"")
    ks._manager._init()

    assert ks.get_key("openai") is None
    ks.save_key("openai", KEY_A, verified=True)
    ks._manager._init()
    assert ks.get_key("openai") == KEY_A


def test_corrupt_primary_and_corrupt_backup_are_never_overwritten(isolated):
    _three_saves(isolated)
    cred, bak = isolated / "credentials.json", isolated / "credentials.json.bak"
    cred.write_bytes(b"\x00" * 64)
    bak.write_bytes(b"\x01" * 64)
    ks._manager._init()

    with pytest.raises(CredentialStoreError) as caught:
        ks.save_key("mistral", "mistral-test-dddddddddddddddddddd")
    assert KEY_A not in str(caught.value)
    assert cred.read_bytes() == b"\x00" * 64
    assert bak.read_bytes() == b"\x01" * 64
    assert not (isolated / "credentials.tmp").exists()


def test_a_truncated_primary_reads_from_backup_and_is_preserved_for_recovery(isolated):
    _three_saves(isolated)
    cred = isolated / "credentials.json"
    half = cred.read_bytes()[:20]
    cred.write_bytes(half)
    ks._manager._init()

    assert ks.get_key("openai") == KEY_A  # served from the backup
    ks.save_key("mistral", "mistral-test-dddddddddddddddddddd", verified=True)
    assert any(f.read_bytes() == half for f in isolated.glob("credentials.json.unreadable-*"))


# ---------------------------------------------------------------------------
# Recovery guidance: a locked keyring is not a damaged store
# ---------------------------------------------------------------------------


def test_a_locked_keyring_is_reported_as_locked_and_repair_refuses_to_touch_the_files(real_crypto):
    tmp, kr = real_crypto
    ks.save_key("openai", KEY_A, verified=True)
    ks.save_key("anthropic", KEY_B, verified=True)
    primary = (tmp / "credentials.json").read_bytes()
    backup = (tmp / "credentials.json.bak").read_bytes()

    kr.fail_reads = True
    ks._manager._init()
    message = ks.store_load_error() or ""
    assert ks._manager.load_key_unavailable
    assert "keyring" in message and "Unlock" in message
    assert "no restart needed" in message
    assert KEY_A not in message and KEY_B not in message

    report = ks.repair_keystore()
    assert report.get("blocked")
    assert not report["restored_backup"] and not report["quarantined"]
    assert (tmp / "credentials.json").read_bytes() == primary
    assert (tmp / "credentials.json.bak").read_bytes() == backup
    assert not list(tmp.glob("credentials.json.unreadable-*"))

    # Unlocking brings the keys back in the same process, without a restart.
    kr.fail_reads = False
    ks._manager._load_failed_at = -1e9
    assert ks.get_key("openai") == KEY_A
    assert ks.store_load_error() is None


def test_a_damaged_store_gets_the_repair_guidance_and_repair_preserves_the_file(isolated):
    ks.save_key("openai", KEY_A, verified=True)
    cred = isolated / "credentials.json"
    garbage = b"\x07" * 64
    cred.write_bytes(garbage)
    (isolated / "credentials.json.bak").unlink(missing_ok=True)
    ks._manager._init()

    message = ks.store_load_error() or ""
    assert not ks._manager.load_key_unavailable
    assert "velune provider repair" in message
    assert "cannot recover" in message  # states what repair does not do
    assert str(cred) in message
    assert KEY_A not in message
    assert cred.read_bytes() == garbage  # reading never alters it

    report = ks.repair_keystore()
    assert not report.get("blocked")
    assert report["quarantined"]
    assert Path(report["quarantined"]).read_bytes() == garbage  # moved, not destroyed


def test_a_store_the_keyring_cannot_open_is_not_called_locked(real_crypto):
    tmp, kr = real_crypto
    ks.save_key("openai", KEY_A, verified=True)
    # The keyring answers, but with a different key: it is not locked, the key changed.
    kr.value = base64.b64encode(b"\x42" * 32).decode()
    ks._manager._init()

    assert ks.store_load_error()
    assert not ks._manager.load_key_unavailable
    assert "locked" not in ks.store_load_error()
