"""Regression tests for the findings of the CI/CD and security audit.

Each test pins one confirmed weakness, so a revert fails loudly. Everything runs
in temp directories with fake keys; no real credential store, keyring, uv or
lifecycle operation is touched.
"""

from __future__ import annotations

import inspect
import io
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from velune.recovery.archive import _safe_extract

WINDOWS = sys.platform == "win32"
POSIX_ONLY = pytest.mark.skipif(WINDOWS, reason="POSIX file modes")
WINDOWS_ONLY = pytest.mark.skipif(not WINDOWS, reason="Windows executable search / ACLs")


# ── S1: a bare executable name must not be resolved from the working directory ────────────────
#
# On Windows, CreateProcess searches the *current directory* for a bare name such as "git" unless
# NoDefaultCurrentDirectoryInExePath is set. Velune runs git automatically when started inside a
# repository, so a planted git.exe in a cloned repository would run without the user typing it.

_SEARCH_VAR = "NoDefaultCurrentDirectoryInExePath"


def _plant_fake_git(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    # whoami.exe stands in for malware: harmless, and its output is never "git version ...".
    shutil.copy(
        Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "whoami.exe",
        directory / "git.exe",
    )


@WINDOWS_ONLY
def test_main_disables_current_directory_executable_search(tmp_path, monkeypatch):
    real_git = shutil.which("git", path=os.environ.get("PATH", ""))
    if not real_git or Path(real_git).resolve().parent == tmp_path.resolve():
        pytest.skip("git is not installed")
    repo = tmp_path / "cloned-repo"
    _plant_fake_git(repo)
    monkeypatch.setenv(_SEARCH_VAR, "x")  # registers cleanup, so the test cannot leak the variable
    monkeypatch.delenv(_SEARCH_VAR)
    monkeypatch.chdir(repo)

    from velune.main import _harden_executable_search

    _harden_executable_search()
    result = subprocess.run(
        ["git", "--version"], capture_output=True, text=True, cwd=repo, timeout=20
    )
    assert result.stdout.startswith("git version"), result.stdout or result.stderr


@WINDOWS_ONLY
def test_the_planted_executable_would_run_without_the_hardening(tmp_path, monkeypatch):
    """Control: proves the attack is real here, so the test above is not passing vacuously."""
    if not shutil.which("git"):
        pytest.skip("git is not installed")
    repo = tmp_path / "cloned-repo"
    _plant_fake_git(repo)
    monkeypatch.setenv(_SEARCH_VAR, "x")
    monkeypatch.delenv(_SEARCH_VAR)
    monkeypatch.chdir(repo)
    result = subprocess.run(
        ["git", "--version"], capture_output=True, text=True, cwd=repo, timeout=20
    )
    assert not result.stdout.startswith("git version")


@POSIX_ONLY
def test_the_hardening_does_nothing_off_windows(monkeypatch):
    monkeypatch.setenv(_SEARCH_VAR, "x")
    monkeypatch.delenv(_SEARCH_VAR)

    from velune.main import _harden_executable_search

    _harden_executable_search()
    assert _SEARCH_VAR not in os.environ


# ── S2: credential files are private from the moment they exist ──────────────────────────────


def test_private_file_is_restricted_before_any_secret_is_written(tmp_path, monkeypatch):
    from velune.core import privatefile

    seen: list[int] = []

    def spy(path: Path) -> None:
        seen.append(path.stat().st_size)  # size at the moment the restriction is applied

    monkeypatch.setattr(privatefile, "restrict_to_current_user", spy)
    target = tmp_path / "credentials.json"
    privatefile.write_private_bytes(target, b"ciphertext")
    assert seen and seen[0] == 0, "the file already held data when it was restricted"
    assert target.read_bytes() == b"ciphertext"
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


@POSIX_ONLY
def test_private_file_is_never_group_or_world_readable_even_with_a_permissive_umask(tmp_path):
    from velune.core import privatefile

    old = os.umask(0)
    try:
        target = tmp_path / "secret.bin"
        privatefile.write_private_bytes(target, b"x")
    finally:
        os.umask(old)
    assert (target.stat().st_mode & 0o777) == 0o600


@POSIX_ONLY
def test_a_stale_permissive_temp_file_is_not_reused(tmp_path):
    from velune.core import privatefile

    stale = tmp_path / "credentials.json.tmp"
    stale.write_bytes(b"old")
    stale.chmod(0o666)
    with privatefile.open_private(stale) as handle:
        handle.write(b"new")
    assert stale.read_bytes() == b"new" and (stale.stat().st_mode & 0o777) == 0o600


@POSIX_ONLY
def test_a_planted_symlink_is_replaced_not_followed(tmp_path):
    from velune.core import privatefile

    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    link = tmp_path / "credentials.json.tmp"
    link.symlink_to(victim)
    with privatefile.open_private(link) as handle:
        handle.write(b"secret")
    assert victim.read_text() == "keep", "the write followed the symlink"
    assert not link.is_symlink() and link.read_bytes() == b"secret"


@WINDOWS_ONLY
def test_icacls_is_run_from_the_system_directory_not_resolved_by_name(tmp_path, monkeypatch):
    from velune.core import privatefile

    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setenv("USERNAME", "someone")
    monkeypatch.setattr(subprocess, "run", fake_run)
    target = tmp_path / "f"
    target.write_bytes(b"")
    privatefile.restrict_to_current_user(target)
    assert calls, "icacls was not invoked"
    exe = Path(calls[0][0])
    assert exe.is_absolute() and exe.name.lower() == "icacls.exe"
    assert exe.parent.name.lower() == "system32" and exe.exists()


def test_credential_store_writes_go_through_the_private_helper(tmp_path, monkeypatch):
    import base64
    from unittest.mock import patch

    from velune.core import privatefile
    from velune.providers import keystore as ks
    from velune.providers.keystore import CredentialManager

    sizes: list[int] = []
    real = privatefile.restrict_to_current_user

    def spy(path: Path) -> None:
        if path.name.endswith(".tmp"):
            sizes.append(path.stat().st_size)
        real(path)

    monkeypatch.setattr(privatefile, "restrict_to_current_user", spy)
    for name in [*ks.all_provider_env_vars(), ks.PREFER_ENV_VAR]:
        monkeypatch.delenv(name, raising=False)
    master = base64.b64decode("QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE=")
    with (
        patch("velune.providers.keystore.user_config_dir", return_value=str(tmp_path)),
        patch("velune.providers.crypto.get_or_create_master_key", return_value=master),
    ):
        CredentialManager._instance = None
        ks._manager._init()
        try:
            ks.save_key("openai", "sk-test-aaaaaaaaaaaaaaaaaaaaaaaa")
        finally:
            CredentialManager._instance = None
            ks._manager._init()
    assert sizes and sizes[0] == 0, (
        "the credential temp file held ciphertext before it was restricted"
    )


def test_persisted_passphrase_is_written_privately_and_atomically(tmp_path, monkeypatch):
    from velune.core import privatefile
    from velune.providers import crypto

    target = tmp_path / "master.passphrase"
    monkeypatch.setattr(crypto, "_passphrase_file_path", lambda: target)
    sizes: list[int] = []
    real = privatefile.restrict_to_current_user
    monkeypatch.setattr(
        privatefile,
        "restrict_to_current_user",
        lambda p: (sizes.append(p.stat().st_size), real(p))[1],
    )
    crypto._persist_passphrase("correct horse battery staple")
    assert sizes and sizes[0] == 0
    assert target.exists() and b"correct horse" not in target.read_bytes()


# ── S3: restoring a backup must not follow links inside the archive ──────────────────────────


def _tar_with_symlink_then_file(tmp_path: Path, outside: Path) -> Path:
    archive = tmp_path / "evil.tar"
    with tarfile.open(archive, "w") as tar:
        link = tarfile.TarInfo("sessions")
        link.type = tarfile.SYMTYPE
        link.linkname = str(outside)
        tar.addfile(link)
        payload = b"planted"
        info = tarfile.TarInfo("sessions/pwned.txt")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return archive


def _pretend_interpreter_has_no_extraction_filter(monkeypatch) -> None:
    """Simulate a pre-backport 3.10/3.11 patch release, where ``filter=`` is a TypeError.

    Old interpreters extract with no checks at all, which ``filter="fully_trusted"`` reproduces.
    """
    from velune.recovery import archive

    original = tarfile.TarFile.extractall
    # A genuinely old interpreter (e.g. some 3.10 builds) has no ``filter`` parameter at all, and
    # its extractall already behaves as "fully_trusted", so forwarding the keyword there would
    # raise the very TypeError this helper is simulating, from inside the helper itself.
    real_accepts_filter = "filter" in inspect.signature(original).parameters

    def old_signature(self, path=".", members=None, *, numeric_owner=False, **kwargs):
        if "filter" in kwargs:
            raise TypeError("extractall() got an unexpected keyword argument 'filter'")
        if real_accepts_filter:
            return original(
                self, path, members, numeric_owner=numeric_owner, filter="fully_trusted"
            )
        return original(self, path, members, numeric_owner=numeric_owner)

    monkeypatch.setattr(tarfile.TarFile, "extractall", old_signature)
    monkeypatch.setattr(archive, "_HAS_EXTRACTION_FILTER", False, raising=False)


def test_symlink_member_cannot_redirect_a_later_member_outside_the_restore_dir(
    tmp_path, monkeypatch
):
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = tmp_path / "restore"
    dest.mkdir()
    archive = _tar_with_symlink_then_file(tmp_path, outside)
    _pretend_interpreter_has_no_extraction_filter(monkeypatch)
    with tarfile.open(archive) as tar, pytest.raises(ValueError, match="link"):
        _safe_extract(tar, dest)
    assert not (outside / "pwned.txt").exists()


def test_hardlink_and_device_members_are_refused_without_the_filter(tmp_path, monkeypatch):
    archive = tmp_path / "evil.tar"
    with tarfile.open(archive, "w") as tar:
        hard = tarfile.TarInfo("h")
        hard.type = tarfile.LNKTYPE
        hard.linkname = "anything"
        tar.addfile(hard)
    dest = tmp_path / "restore"
    dest.mkdir()
    _pretend_interpreter_has_no_extraction_filter(monkeypatch)
    with tarfile.open(archive) as tar, pytest.raises(ValueError, match="link"):
        _safe_extract(tar, dest)


def test_a_typeerror_raised_during_filtered_extraction_is_not_retried_without_the_filter(
    tmp_path, monkeypatch
):
    from velune.recovery import archive

    if not getattr(archive, "_HAS_EXTRACTION_FILTER", hasattr(tarfile, "data_filter")):
        pytest.skip("this interpreter has no extraction filter")
    calls: list[dict] = []

    def boom(self, path=".", members=None, **kwargs):
        calls.append(kwargs)
        raise TypeError("unrelated failure inside extraction")

    monkeypatch.setattr(tarfile.TarFile, "extractall", boom)
    dest = tmp_path / "restore"
    dest.mkdir()
    archive = tmp_path / "ok.tar"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo("a.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with tarfile.open(archive) as tar, pytest.raises(TypeError):
        _safe_extract(tar, dest)
    assert len(calls) == 1 and calls[0].get("filter") == "data", "extraction was retried unfiltered"


def test_ordinary_regular_files_still_restore_without_the_filter(tmp_path, monkeypatch):
    dest = tmp_path / "restore"
    dest.mkdir()
    archive = tmp_path / "ok.tar"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo("sessions/s.json")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"{}"))
    _pretend_interpreter_has_no_extraction_filter(monkeypatch)
    with tarfile.open(archive) as tar:
        _safe_extract(tar, dest)
    assert (dest / "sessions" / "s.json").read_bytes() == b"{}"


# ── S4: the SQL identifier guard must be a full match ────────────────────────────────────────


@pytest.mark.parametrize("name", ["users\n", "users\r\n", "a b", "a'b", "a;b", "", "tab\tname"])
def test_sql_identifier_guard_rejects_anything_but_plain_identifiers(name):
    from velune.resources import sql_base

    guard = sql_base._IDENTIFIER
    assert guard.fullmatch(name) is None


@pytest.mark.parametrize("name", ["users", "public", "Table_1", "_x"])
def test_sql_identifier_guard_accepts_plain_identifiers(name):
    from velune.resources import sql_base

    assert sql_base._IDENTIFIER.fullmatch(name)


def test_the_connector_guard_rejects_a_trailing_newline():
    from velune.resources.postgres.connector import PostgresConnector

    connector = PostgresConnector.__new__(PostgresConnector)
    assert connector._validate_identifier("users") is True
    assert connector._validate_identifier("users\n") is False
