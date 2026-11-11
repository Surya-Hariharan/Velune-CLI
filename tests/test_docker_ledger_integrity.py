"""The Docker ownership ledger is authenticated, atomic and merge-safe (VAIR A5)."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from velune.resources.docker.ownership import (
    DockerOwnership,
    DockerOwnershipStore,
    Facts,
    default_ledger_key,
)

KEY = b"\x01" * 32
OTHER_KEY = b"\x02" * 32


def store(tmp_path, *, session="s1", daemon="D1", key=KEY, workspace=None, path=None):
    s = DockerOwnershipStore(
        session,
        workspace or tmp_path,
        path=path or tmp_path / "state" / "own.json",
        key_provider=lambda: key,
    )
    s.bind_daemon(daemon)
    return s


def labelled(s, ident="c1", fingerprint="t0", name="web"):
    return Facts("container", ident, name, fingerprint, dict(s.labels()))


def file_of(tmp_path) -> Path:
    return tmp_path / "state" / "own.json"


def load(tmp_path) -> dict:
    return json.loads(file_of(tmp_path).read_text(encoding="utf-8"))


def leftovers(tmp_path):
    return sorted(p.name for p in (tmp_path / "state").iterdir())


# ── Round trip ──────────────────────────────────────────────────────────────


def test_a_saved_record_is_authenticated_and_survives_a_restart(tmp_path):
    first = store(tmp_path)
    first.record_created(labelled(first))
    section = load(tmp_path)["daemons"]["D1"]
    assert section["mac"] and len(section["mac"]) == 64 and section["records"][0]["id"] == "c1"
    again = store(tmp_path, session="s2")
    assert again.classify(labelled(again)) is DockerOwnership.PREVIOUS  # a verified earlier session


def test_the_mac_does_not_expose_the_key_and_the_file_holds_no_secret(tmp_path):
    s = store(tmp_path)
    s.record_created(labelled(s))
    assert KEY.hex() not in file_of(tmp_path).read_text(encoding="utf-8")


# ── Forgery and tampering: nothing in an unverifiable file is trusted ───────


def test_editing_a_record_breaks_the_seal(tmp_path):
    s = store(tmp_path)
    s.record_created(labelled(s))
    doc = load(tmp_path)
    doc["daemons"]["D1"]["records"][0]["session"] = "forged"
    file_of(tmp_path).write_text(json.dumps(doc), encoding="utf-8")
    again = store(tmp_path, session="s1")
    assert again.classify(labelled(again)) is DockerOwnership.EXTERNAL
    assert any(".tampered-" in name for name in leftovers(tmp_path))  # kept aside, not read


def test_a_record_added_by_hand_is_not_believed(tmp_path):
    s = store(tmp_path)
    s.record_created(labelled(s, "real"))
    doc = load(tmp_path)
    doc["daemons"]["D1"]["records"].append(
        {
            "kind": "container",
            "id": "planted",
            "name": "x",
            "fingerprint": "t0",
            "session": "s1",
            "recorded_at": 1,
            "project": "",
        }
    )
    file_of(tmp_path).write_text(json.dumps(doc), encoding="utf-8")
    again = store(tmp_path)
    assert again.classify(labelled(again, "planted")) is DockerOwnership.EXTERNAL
    assert again.classify(labelled(again, "real")) is DockerOwnership.EXTERNAL  # all or nothing


def test_a_record_deleted_by_hand_also_breaks_the_seal(tmp_path):
    s = store(tmp_path)
    s.record_created(labelled(s, "a"))
    s.record_created(labelled(s, "b"))
    doc = load(tmp_path)
    doc["daemons"]["D1"]["records"].pop()
    file_of(tmp_path).write_text(json.dumps(doc), encoding="utf-8")
    again = store(tmp_path)
    assert again.owned() == []


def test_a_file_without_a_seal_is_not_trusted(tmp_path):
    (tmp_path / "state").mkdir()
    file_of(tmp_path).write_text(
        json.dumps(
            {
                "version": 2,
                "daemons": {
                    "D1": {
                        "records": [
                            {
                                "kind": "container",
                                "id": "c1",
                                "name": "w",
                                "fingerprint": "t0",
                                "session": "s1",
                                "recorded_at": 1,
                                "project": "",
                            }
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    s = store(tmp_path)
    assert s.owned() == [] and s.classify(labelled(s)) is DockerOwnership.EXTERNAL


def test_the_old_unauthenticated_format_is_set_aside(tmp_path):
    (tmp_path / "state").mkdir()
    file_of(tmp_path).write_text(
        json.dumps(
            {
                "version": 1,
                "daemons": {
                    "D1": [
                        {
                            "kind": "container",
                            "id": "c1",
                            "name": "w",
                            "fingerprint": "t0",
                            "session": "s1",
                            "recorded_at": 1,
                            "project": "",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    s = store(tmp_path)
    assert s.owned() == [] and any(".unauthenticated-" in n for n in leftovers(tmp_path))
    s.record_created(labelled(s))  # and a fresh, sealed file can be written
    assert load(tmp_path)["version"] == 2


def test_a_different_key_means_the_file_is_not_ours(tmp_path):
    s = store(tmp_path)
    s.record_created(labelled(s))
    other = store(tmp_path, key=OTHER_KEY)
    assert other.owned() == [] and any(".tampered-" in n for n in leftovers(tmp_path))


def test_a_section_copied_to_another_daemon_does_not_verify(tmp_path):
    s = store(tmp_path, daemon="D1")
    s.record_created(labelled(s))
    doc = load(tmp_path)
    doc["daemons"]["D2"] = doc["daemons"]["D1"]
    file_of(tmp_path).write_text(json.dumps(doc), encoding="utf-8")
    assert store(tmp_path, daemon="D2").owned() == []


def test_a_file_copied_from_another_workspace_does_not_verify(tmp_path):
    a = tmp_path / "ws_a"
    b = tmp_path / "ws_b"
    a.mkdir()
    b.mkdir()
    shared = tmp_path / "shared.json"
    s = store(tmp_path, workspace=a, path=shared)
    s.record_created(labelled(s))
    assert store(tmp_path, workspace=b, path=shared).owned() == []


def test_a_corrupt_file_is_set_aside_and_a_new_one_can_be_written(tmp_path):
    (tmp_path / "state").mkdir()
    file_of(tmp_path).write_text("{not json", encoding="utf-8")
    s = store(tmp_path)
    assert s.owned() == [] and any(".corrupt-" in n for n in leftovers(tmp_path))
    s.record_created(labelled(s))
    assert store(tmp_path, session="s2").owned()[0].id == "c1"


def test_a_missing_file_is_simply_empty(tmp_path):
    assert store(tmp_path).owned() == []


def test_a_garbage_section_does_not_crash(tmp_path):
    (tmp_path / "state").mkdir()
    for section in (None, 5, {"records": "x", "mac": 1}, {"records": [], "mac": "00"}):
        file_of(tmp_path).write_text(json.dumps({"version": 2, "daemons": {"D1": section}}))
        assert store(tmp_path).owned() == []
        for stale in tmp_path.joinpath("state").glob("own.json.*"):
            stale.unlink()


# ── No key: fail safe ───────────────────────────────────────────────────────


def test_without_a_key_records_stay_in_memory_and_nothing_is_trusted_from_disk(tmp_path):
    keyed = store(tmp_path)
    keyed.record_created(labelled(keyed))
    before = file_of(tmp_path).read_text(encoding="utf-8")
    keyless = store(tmp_path, key=None)  # the keystore is unavailable
    assert keyless.owned() == []  # the file cannot be verified
    keyless.record_created(labelled(keyless, "mine"))
    assert keyless.classify(labelled(keyless, "mine")) is DockerOwnership.VELUNE  # this session
    assert file_of(tmp_path).read_text(encoding="utf-8") == before  # and nothing was written


def test_the_default_key_is_created_once_in_the_keystore_and_is_stable(monkeypatch):
    saved: dict[str, str] = {}
    monkeypatch.setattr("velune.providers.keystore.get_key", lambda k: saved.get(k))
    monkeypatch.setattr("velune.providers.keystore.save_key", lambda k, v: saved.update({k: v}))
    first = (
        default_ledger_key()
    )  # the real function: the autouse fixture only patches the module attribute
    second = default_ledger_key()
    assert first is not None and len(first) == 32 and first == second
    assert list(saved) == [
        "resource:docker:ledger-key"
    ]  # one namespaced entry, never a provider id


def test_a_keystore_failure_means_no_key_not_an_exception(monkeypatch):
    def locked(_key):
        raise OSError("keystore locked")

    monkeypatch.setattr("velune.providers.keystore.get_key", locked)
    assert default_ledger_key() is None


# ── Atomic, locked, merged writes ───────────────────────────────────────────


def test_a_save_leaves_no_temporary_files_behind(tmp_path):
    s = store(tmp_path)
    for i in range(5):
        s.record_created(labelled(s, f"c{i}"))
    assert leftovers(tmp_path) == ["own.json", "own.json.lock"]


def test_a_failed_replace_keeps_the_old_file_intact_and_cleans_up(tmp_path, monkeypatch):
    s = store(tmp_path)
    s.record_created(labelled(s, "kept"))
    good = file_of(tmp_path).read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    s.record_created(labelled(s, "lost"))  # must not raise
    monkeypatch.undo()
    assert file_of(tmp_path).read_text(encoding="utf-8") == good
    assert [n for n in leftovers(tmp_path) if n.endswith(".tmp")] == []
    assert store(tmp_path, session="s2").owned()[0].id == "kept"


def test_the_data_is_flushed_to_disk_before_the_swap(tmp_path, monkeypatch):
    synced: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
    s = store(tmp_path)
    s.record_created(labelled(s))
    assert synced


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_the_record_file_is_owner_only(tmp_path):
    s = store(tmp_path)
    s.record_created(labelled(s))
    assert (file_of(tmp_path).stat().st_mode & 0o777) == 0o600
    assert (file_of(tmp_path).parent.stat().st_mode & 0o777) == 0o700


def test_two_processes_recording_at_once_do_not_lose_each_others_records(tmp_path):
    a = store(tmp_path, session="proc-a")
    b = store(tmp_path, session="proc-b")  # loaded before a wrote anything
    a.record_created(labelled(a, "from-a"))
    b.record_created(labelled(b, "from-b"))  # b merges what a recorded
    c = store(tmp_path, session="proc-c")
    assert {r.id for r in c.owned()} == {"from-a", "from-b"}


def test_many_threads_recording_concurrently_lose_nothing(tmp_path):
    stores = [store(tmp_path, session=f"s{i}") for i in range(8)]
    errors: list[BaseException] = []

    def work(i: int) -> None:
        try:
            for j in range(5):
                stores[i].record_created(labelled(stores[i], f"c{i}-{j}"))
        except BaseException as exc:  # pragma: no cover - the assertion below reports it
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(store(tmp_path, session="check").owned()) == 40


def test_a_released_record_is_not_resurrected_by_the_next_save(tmp_path):
    s = store(tmp_path)
    s.record_created(labelled(s, "old"))
    s.record_created(labelled(s, "keep"))
    s.release("container", "old")
    s.record_created(labelled(s, "new"))
    assert {r.id for r in store(tmp_path, session="x").owned()} == {"keep", "new"}


def test_an_unobtainable_lock_does_not_crash_and_memory_still_works(tmp_path, monkeypatch):
    from velune.core import filelock

    def locked(self, timeout=5.0):
        raise filelock.FileLockTimeout("busy")

    monkeypatch.setattr(filelock.FileLock, "acquire", locked)
    s = store(tmp_path)
    s.record_created(labelled(s))
    assert s.classify(labelled(s)) is DockerOwnership.VELUNE
    assert not file_of(tmp_path).exists()


# ── Reconciliation still works on top of a sealed file ──────────────────────


def test_reconciling_drops_vanished_records_and_the_file_stays_sealed(tmp_path):
    s = store(tmp_path)
    s.record_created(labelled(s, "gone"))
    s.record_created(labelled(s, "stay"))
    report = s.reconcile({"container": {"stay": labelled(s, "stay")}})
    assert report["missing"] == ["container:gone"]
    reopened = store(tmp_path, session="s9")
    assert [r.id for r in reopened.owned()] == ["stay"]
