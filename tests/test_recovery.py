"""Tests for the unified backup / restore / recover subsystem."""

from __future__ import annotations

import json
import sqlite3
import tarfile

import pytest

from velune.core.paths import cognitive_db_path
from velune.core.trust import trust_file_path
from velune.recovery import create_backup, restore_backup
from velune.recovery.archive import MANIFEST_NAME

ROUNDTRIP_SUBSYSTEMS = {"sessions", "config", "memory", "trust"}


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
