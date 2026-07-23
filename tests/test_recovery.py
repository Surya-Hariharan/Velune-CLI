"""Tests for the unified backup / restore / recover subsystem."""

from __future__ import annotations

import base64
import json
import sqlite3
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from velune.cli import design
from velune.cli.handlers.recovery import _split_args, cmd_restore
from velune.core.paths import cognitive_db_path
from velune.core.trust import trust_file_path
from velune.recovery import archive_has_encrypted_secrets, create_backup, restore_backup
from velune.recovery.archive import MANIFEST_NAME, _safe_extract

ROUNDTRIP_SUBSYSTEMS = {"sessions", "config", "memory", "trust"}


# ── Windows path argument splitting ──────────────────────────────────────────
#
# shlex.split() defaults to POSIX mode, where backslash is an escape
# character. On Windows that silently deletes the backslashes from paths
# like `/backup C:\Users\me\backup.tar.gz`, so the command reports success
# against a mangled path instead of the one the user asked for.


def test_split_args_preserves_windows_backslash_path(monkeypatch):
    monkeypatch.setattr("velune.cli.handlers.recovery.os.name", "nt")
    tokens = _split_args(r"C:\Users\surya\AppData\Temp\velune-backup.tar.gz")
    assert tokens == [r"C:\Users\surya\AppData\Temp\velune-backup.tar.gz"]


def test_split_args_strips_quotes_around_windows_path(monkeypatch):
    monkeypatch.setattr("velune.cli.handlers.recovery.os.name", "nt")
    tokens = _split_args(r'"C:\Users\surya\My Backups\velune-backup.tar.gz"')
    assert tokens == [r"C:\Users\surya\My Backups\velune-backup.tar.gz"]


def test_split_args_still_parses_flags_on_windows(monkeypatch):
    monkeypatch.setattr("velune.cli.handlers.recovery.os.name", "nt")
    tokens = _split_args(r"C:\backup.tar.gz --include sessions,config --overwrite")
    assert tokens == [r"C:\backup.tar.gz", "--include", "sessions,config", "--overwrite"]


def test_split_args_empty_string_returns_empty_list():
    assert _split_args("") == []


def test_split_args_posix_paths_unaffected(monkeypatch):
    monkeypatch.setattr("velune.cli.handlers.recovery.os.name", "posix")
    tokens = _split_args("/home/user/backup.tar.gz --overwrite")
    assert tokens == ["/home/user/backup.tar.gz", "--overwrite"]


@pytest.fixture
def mock_keystore(tmp_path):
    """Isolate the provider credential store under tmp_path with a fixed key."""
    from velune.providers.keystore import CredentialManager

    with (
        patch("velune.providers.keystore.user_config_dir", return_value=str(tmp_path)),
        patch("velune.providers.crypto.get_or_create_master_key") as mock_get_key,
    ):
        mock_get_key.return_value = base64.b64decode("QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE=")
        CredentialManager._instance = None
        from velune.providers.keystore import _manager

        _manager._init()

        yield

        CredentialManager._instance = None
        _manager._init()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolate all Velune state under tmp_path (data root + sessions dir)."""
    data_home = tmp_path / "data"
    monkeypatch.setenv("VELUNE_DATA_HOME", str(data_home))

    sessions_dir = tmp_path / "home" / ".velune" / "sessions"
    monkeypatch.setattr("velune.cli.sessions.DEFAULT_SESSIONS_DIR", sessions_dir)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    return {"workspace": workspace, "sessions_dir": sessions_dir, "data_home": data_home}


def _seed_state(env) -> dict:
    """Create one of every recoverable artifact and return their paths/values."""
    from velune.cli.sessions import SessionStore

    ws = env["workspace"]

    # Session
    store = SessionStore(root=env["sessions_dir"])
    conv = [
        {"role": "user", "content": "implement the JWT refresh flow"},
        {"role": "assistant", "content": "Here is the plan..."},
    ]
    meta = store.save(conv, workspace=str(ws), model_id="test/model", session_id="sess0001")

    # Config
    config_file = ws / "velune.toml"
    config_file.write_text('[project]\nname = "demo"\n', encoding="utf-8")

    # Trust list
    trust_path = trust_file_path()
    trust_path.parent.mkdir(parents=True, exist_ok=True)
    trust_path.write_text(
        json.dumps({"version": 2, "directories": {str(ws): {}}}), encoding="utf-8"
    )

    # Memory: a tiny SQLite cognitive core
    db_path = cognitive_db_path(ws)
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE turns (id INTEGER PRIMARY KEY, content TEXT)")
    conn.execute("INSERT INTO turns (content) VALUES ('remembered fact')")
    conn.commit()
    conn.close()

    return {
        "session_id": meta.id,
        "conv": conv,
        "config_file": config_file,
        "trust_path": trust_path,
        "db_path": db_path,
    }


def test_backup_creates_archive_with_manifest(env):
    _seed_state(env)
    dest = env["workspace"] / "snap.tar.gz"

    result = create_backup(dest, include=ROUNDTRIP_SUBSYSTEMS, workspace=env["workspace"])

    assert dest.is_file()
    assert result.size_bytes > 0
    with tarfile.open(dest, "r:gz") as tar:
        names = tar.getnames()
        manifest = json.loads(tar.extractfile(f"./{MANIFEST_NAME}").read())

    assert ROUNDTRIP_SUBSYSTEMS <= set(manifest["subsystems"])
    assert any("sessions/sess0001.json" in n for n in names)
    assert manifest["subsystems"]["memory"]["db"] is True


def test_backup_restore_roundtrip(env):
    seed = _seed_state(env)
    dest = env["workspace"] / "snap.tar.gz"
    create_backup(dest, include=ROUNDTRIP_SUBSYSTEMS, workspace=env["workspace"])

    # Wipe every live artifact.
    (env["sessions_dir"] / f"{seed['session_id']}.json").unlink()
    seed["config_file"].unlink()
    seed["trust_path"].unlink()
    seed["db_path"].unlink()

    result = restore_backup(
        dest, include=ROUNDTRIP_SUBSYSTEMS, overwrite=True, workspace=env["workspace"]
    )
    assert not result.dry_run

    # Session content returns intact.
    from velune.cli.sessions import SessionStore

    loaded = SessionStore(root=env["sessions_dir"]).load(seed["session_id"])
    assert loaded is not None
    _meta, conv = loaded
    assert conv == seed["conv"]

    # Config + trust files restored.
    assert seed["config_file"].is_file()
    assert seed["trust_path"].is_file()

    # SQLite DB restored and queryable.
    assert seed["db_path"].is_file()
    conn = sqlite3.connect(str(seed["db_path"]))
    row = conn.execute("SELECT content FROM turns").fetchone()
    conn.close()
    assert row[0] == "remembered fact"


def test_restore_dry_run_writes_nothing(env):
    seed = _seed_state(env)
    dest = env["workspace"] / "snap.tar.gz"
    create_backup(dest, include={"sessions"}, workspace=env["workspace"])

    session_file = env["sessions_dir"] / f"{seed['session_id']}.json"
    session_file.unlink()

    result = restore_backup(dest, include={"sessions"}, dry_run=True, workspace=env["workspace"])

    assert result.dry_run
    assert result.restored["sessions"]  # plan reported
    assert not session_file.exists()  # but nothing written


def test_restore_skips_existing_without_overwrite(env):
    _seed_state(env)
    dest = env["workspace"] / "snap.tar.gz"
    create_backup(dest, include={"sessions"}, workspace=env["workspace"])

    # File still present → should be skipped.
    result = restore_backup(dest, include={"sessions"}, overwrite=False, workspace=env["workspace"])
    assert result.skipped.get("sessions")
    assert not result.restored.get("sessions")


def test_unknown_subsystem_rejected(env):
    dest = env["workspace"] / "snap.tar.gz"
    with pytest.raises(ValueError):
        create_backup(dest, include={"nonsense"}, workspace=env["workspace"])


def test_restore_missing_archive(env):
    with pytest.raises(FileNotFoundError):
        restore_backup(env["workspace"] / "nope.tar.gz")


# ── /restore confirmation gate ───────────────────────────────────────────────
#
# `cmd_restore` used to write straight into the user's Velune state with no
# confirmation at all — the REPL counterpart to the CLI's `restore_cmd`, which
# itself defaulted to *accept* on a bare Enter. Found during a production-
# readiness audit pass; both are now behind `confirm_destructive` /
# default=False, matching the guard `/memory clear` already got in an earlier
# pass (see test_slash_command_permissions.py).


def _make_repl(workspace):
    repl = MagicMock()
    repl.console = MagicMock()
    repl.container.get.side_effect = lambda key: {"runtime.workspace": workspace}.get(key)
    return repl


async def test_cmd_restore_does_nothing_without_confirmation(env):
    seed = _seed_state(env)
    dest = env["workspace"] / "snap.tar.gz"
    create_backup(dest, include={"sessions"}, workspace=env["workspace"])
    session_file = env["sessions_dir"] / f"{seed['session_id']}.json"
    session_file.unlink()

    repl = _make_repl(env["workspace"])
    with patch("velune.cli.handlers.confirm.confirm_destructive", AsyncMock(return_value=False)):
        await cmd_restore(repl, f"{dest} --overwrite")

    assert not session_file.exists(), "declined confirmation must not write anything"
    repl.console.print.assert_any_call(f"[{design.MUTED}]Aborted.[/{design.MUTED}]")


async def test_cmd_restore_proceeds_once_confirmed(env):
    seed = _seed_state(env)
    dest = env["workspace"] / "snap.tar.gz"
    create_backup(dest, include={"sessions"}, workspace=env["workspace"])
    session_file = env["sessions_dir"] / f"{seed['session_id']}.json"
    session_file.unlink()

    repl = _make_repl(env["workspace"])
    with patch("velune.cli.handlers.confirm.confirm_destructive", AsyncMock(return_value=True)):
        await cmd_restore(repl, f"{dest} --overwrite")

    assert session_file.exists()


async def test_cmd_restore_dry_run_skips_the_confirmation_prompt(env):
    """A --dry-run writes nothing, so there's nothing to confirm."""
    seed = _seed_state(env)
    dest = env["workspace"] / "snap.tar.gz"
    create_backup(dest, include={"sessions"}, workspace=env["workspace"])
    session_file = env["sessions_dir"] / f"{seed['session_id']}.json"
    session_file.unlink()

    repl = _make_repl(env["workspace"])
    confirm_mock = AsyncMock(return_value=False)
    with patch("velune.cli.handlers.confirm.confirm_destructive", confirm_mock):
        await cmd_restore(repl, f"{dest} --dry-run")

    confirm_mock.assert_not_awaited()


# ── Provider secrets: never written in the clear ────────────────────────────


def test_backup_defaults_to_masked_provider_keys(env, mock_keystore):
    from velune.providers.keystore import save_key

    save_key("openai", "sk-super-secret")
    dest = env["workspace"] / "snap.tar.gz"

    result = create_backup(dest, include={"providers"}, workspace=env["workspace"])

    assert result.with_secrets is False
    with tarfile.open(dest, "r:gz") as tar:
        names = tar.getnames()
        assert any(n.endswith("providers/providers.json") for n in names)
        assert not any(n.endswith("providers.json.enc") for n in names)
        payload = json.loads(tar.extractfile("./providers/providers.json").read())

    assert payload["providers"]["openai"]["key"] == "***"
    # The raw archive bytes must never contain the plaintext secret.
    assert b"sk-super-secret" not in dest.read_bytes()


def test_backup_with_secrets_requires_passphrase(env, mock_keystore):
    from velune.providers.keystore import save_key

    save_key("openai", "sk-super-secret")
    dest = env["workspace"] / "snap.tar.gz"

    with pytest.raises(ValueError):
        create_backup(dest, include={"providers"}, with_secrets=True, workspace=env["workspace"])


def test_backup_with_secrets_encrypts_keys(env, mock_keystore):
    from velune.providers.keystore import save_key

    save_key("openai", "sk-super-secret")
    dest = env["workspace"] / "snap.tar.gz"

    result = create_backup(
        dest,
        include={"providers"},
        with_secrets=True,
        secrets_passphrase="correct horse battery staple",
        workspace=env["workspace"],
    )

    assert result.with_secrets is True
    assert archive_has_encrypted_secrets(dest)
    with tarfile.open(dest, "r:gz") as tar:
        names = tar.getnames()
        assert any(n.endswith("providers.json.enc") for n in names)
        assert not any(n.endswith("providers/providers.json") for n in names)

    # No plaintext trace of the secret anywhere in the archive bytes.
    assert b"sk-super-secret" not in dest.read_bytes()

    # The manifest summary must never carry the key material either.
    with tarfile.open(dest, "r:gz") as tar:
        manifest = json.loads(tar.extractfile(f"./{MANIFEST_NAME}").read())
    assert manifest["subsystems"]["providers"]["providers"] == ["openai"]
    assert "sk-super-secret" not in json.dumps(manifest)


def test_restore_encrypted_secrets_roundtrip(env, mock_keystore):
    from velune.providers.keystore import delete_key, get_key, save_key

    save_key("openai", "sk-super-secret")
    dest = env["workspace"] / "snap.tar.gz"
    create_backup(
        dest,
        include={"providers"},
        with_secrets=True,
        secrets_passphrase="correct horse battery staple",
        workspace=env["workspace"],
    )

    delete_key("openai")
    assert get_key("openai") is None

    result = restore_backup(
        dest,
        include={"providers"},
        overwrite=True,
        secrets_passphrase="correct horse battery staple",
        workspace=env["workspace"],
    )

    assert "openai" in result.restored["providers"]
    assert get_key("openai") == "sk-super-secret"


def test_restore_encrypted_secrets_wrong_passphrase_skips(env, mock_keystore):
    from velune.providers.keystore import delete_key, get_key, save_key

    save_key("openai", "sk-super-secret")
    dest = env["workspace"] / "snap.tar.gz"
    create_backup(
        dest,
        include={"providers"},
        with_secrets=True,
        secrets_passphrase="correct horse battery staple",
        workspace=env["workspace"],
    )

    delete_key("openai")

    result = restore_backup(
        dest,
        include={"providers"},
        overwrite=True,
        secrets_passphrase="wrong passphrase",
        workspace=env["workspace"],
    )

    assert not result.restored.get("providers")
    assert result.skipped["providers"] == ["providers (wrong passphrase)"]
    assert get_key("openai") is None


def test_restore_encrypted_secrets_without_passphrase_skips(env, mock_keystore):
    from velune.providers.keystore import save_key

    save_key("openai", "sk-super-secret")
    dest = env["workspace"] / "snap.tar.gz"
    create_backup(
        dest,
        include={"providers"},
        with_secrets=True,
        secrets_passphrase="correct horse battery staple",
        workspace=env["workspace"],
    )

    result = restore_backup(dest, include={"providers"}, overwrite=True, workspace=env["workspace"])

    assert not result.restored.get("providers")
    assert result.skipped["providers"] == ["providers (passphrase required to decrypt secrets)"]


# ── Autosave / crash recovery ────────────────────────────────────────────────


def test_autosave_orphan_lifecycle(env):
    from velune.cli.sessions import SessionStore

    store = SessionStore(root=env["sessions_dir"])
    conv = [
        {"role": "user", "content": "draft the migration"},
        {"role": "assistant", "content": "step one..."},
    ]
    store.autosave(conv, session_id="live0001", workspace=str(env["workspace"]), model_id="m")

    orphans = store.list_orphaned_autosaves()
    assert [m.id for m in orphans] == ["live0001"]

    # Recovering promotes it to a real session and clears the sidecar.
    saved = store.recover_autosave("live0001")
    assert saved is not None and saved.id == "live0001"
    assert store.list_orphaned_autosaves() == []

    loaded = store.load("live0001")
    assert loaded is not None
    assert loaded[1] == conv


def test_clear_autosave_marks_clean_exit(env):
    from velune.cli.sessions import SessionStore

    store = SessionStore(root=env["sessions_dir"])
    store.autosave([{"role": "user", "content": "x"}], session_id="s", workspace="", model_id="m")
    assert store.list_orphaned_autosaves()
    store.clear_autosave("s")
    assert store.list_orphaned_autosaves() == []


def test_discard_autosave(env):
    from velune.cli.sessions import SessionStore

    store = SessionStore(root=env["sessions_dir"])
    store.autosave([{"role": "user", "content": "x"}], session_id="s", workspace="", model_id="m")
    assert store.discard_autosave("s") is True
    assert store.discard_autosave("s") is False
    assert store.list_orphaned_autosaves() == []


# ── _safe_extract: zip-slip / path-escape protection ────────────────────────
#
# The original guard was `str(member_path).startswith(str(dest))` — a string
# *prefix* test, not a containment test. `/x/dest-evil/f` starts with the
# string `/x/dest`, without being inside `dest` at all, so a crafted member
# name could still land a file next to (not inside) the restore directory.
# Found untested during a production-readiness audit pass; fixed to match the
# containment check `PathGuard.validate()` already uses elsewhere.


def _build_tar_with_member(tmp_path, member_name: str, content: bytes = b"payload") -> Path:
    import io
    import tarfile as tarfile_mod

    archive = tmp_path / "malicious.tar"
    with tarfile_mod.open(archive, "w") as tar:
        info = tarfile_mod.TarInfo(name=member_name)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return archive


def test_safe_extract_rejects_dotdot_traversal(tmp_path):
    dest = tmp_path / "restore-dest"
    dest.mkdir()
    archive = _build_tar_with_member(tmp_path, "../../etc/passwd")

    import tarfile as tarfile_mod

    with tarfile_mod.open(archive) as tar, pytest.raises(ValueError, match="Unsafe path"):
        _safe_extract(tar, dest)


def test_safe_extract_rejects_sibling_directory_sharing_a_string_prefix(tmp_path):
    """Regression test for the exact bug the naive startswith() check missed:
    a sibling directory whose name has `dest`'s name as a string prefix."""
    dest = tmp_path / "velune-restore"
    dest.mkdir()
    (tmp_path / "velune-restore-evil").mkdir()
    archive = _build_tar_with_member(tmp_path, "../velune-restore-evil/planted")

    import tarfile as tarfile_mod

    with tarfile_mod.open(archive) as tar, pytest.raises(ValueError, match="Unsafe path"):
        _safe_extract(tar, dest)
    assert not (tmp_path / "velune-restore-evil" / "planted").exists()


def test_safe_extract_allows_legitimate_nested_members(tmp_path):
    dest = tmp_path / "restore-dest"
    dest.mkdir()
    archive = _build_tar_with_member(tmp_path, "sessions/session1.json", b'{"ok": true}')

    import tarfile as tarfile_mod

    with tarfile_mod.open(archive) as tar:
        _safe_extract(tar, dest)

    extracted = dest / "sessions" / "session1.json"
    assert extracted.is_file()
    assert extracted.read_bytes() == b'{"ok": true}'
