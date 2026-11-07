"""Lifecycle journal persistence (P2.1). Temp directories only; no uv, no real app data."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from velune.lifecycle import journal as jr
from velune.lifecycle.errors import ExitCode, LifecycleError
from velune.lifecycle.ownership import OwnershipKind, OwnershipReport


def confirmed(**kw) -> OwnershipReport:
    base: dict = {
        "kind": OwnershipKind.UV_TOOL,
        "reasons": (),
        "prefix": "/t/velune-cli",
        "version": "0.9.9",
        "specifier": "==0.9.9",
        "extras": ("rag",),
        "python_base": "/usr/bin/python3.12",
    }
    return OwnershipReport(**(base | kw))


def entry(**kw) -> jr.JournalEntry:
    return jr.new_entry(
        kw.pop("operation", "update"),
        kw.pop("report", confirmed()),
        python_version="3.12.4",
        to_version=kw.pop("to_version", "0.9.10"),
        now=datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc),
    )


def test_round_trip_preserves_everything_needed_to_roll_back(tmp_path):
    path = tmp_path / "lifecycle" / "journal.json"
    jr.write_journal(entry(), path)
    read = jr.read_journal(path)
    assert read.state == "ok"
    e = read.entry
    assert e == entry()
    assert (e.from_version, e.specifier, e.extras) == ("0.9.9", "==0.9.9", ("rag",))
    assert (e.python_version, e.python_base, e.to_version) == (
        "3.12.4",
        "/usr/bin/python3.12",
        "0.9.10",
    )
    assert e.started_at == "2026-11-07T12:00:00Z" and e.phase == "planned" and e.installer == "uv"


def test_creates_only_its_own_directory(tmp_path):
    path = tmp_path / "lifecycle" / "journal.json"
    jr.write_journal(entry(), path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["lifecycle"]
    assert sorted(p.name for p in path.parent.iterdir()) == ["journal.json"]  # no temp leftovers


def test_missing_journal(tmp_path):
    assert jr.read_journal(tmp_path / "nope.json").state == "missing"


def test_refuses_to_overwrite_an_existing_journal(tmp_path):
    path = tmp_path / "journal.json"
    jr.write_journal(entry(), path)
    before = path.read_bytes()
    with pytest.raises(LifecycleError) as caught:
        jr.write_journal(entry(operation="repair"), path)
    assert caught.value.code is ExitCode.REFUSED
    assert path.read_bytes() == before


def test_does_not_touch_an_unrelated_file_at_the_path(tmp_path):
    path = tmp_path / "journal.json"
    path.write_text("someone else's data")
    with pytest.raises(LifecycleError):
        jr.write_journal(entry(), path)
    assert path.read_text() == "someone else's data"


def test_leaves_sibling_files_alone(tmp_path):
    (tmp_path / "other.txt").write_text("keep")
    jr.write_journal(entry(), tmp_path / "journal.json")
    assert (tmp_path / "other.txt").read_text() == "keep"


def test_refuses_when_target_is_a_directory(tmp_path):
    (tmp_path / "journal.json").mkdir()
    with pytest.raises(LifecycleError):
        jr.write_journal(entry(), tmp_path / "journal.json")
    assert jr.read_journal(tmp_path / "journal.json").state == "malformed"


@pytest.mark.skipif(os.name == "nt", reason="creating symlinks needs privileges on Windows")
def test_symlink_is_never_followed(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    link = tmp_path / "journal.json"
    link.symlink_to(victim)
    with pytest.raises(LifecycleError):
        jr.write_journal(entry(), link)
    assert victim.read_text() == "precious"
    assert jr.read_journal(link).state == "malformed"


@pytest.mark.parametrize(
    "content",
    ["", "{not json", "[]", "42", '"text"', "null", "\x00\x01\x02", '{"schema_version": 2}'],
)
def test_malformed_content_is_reported_not_raised(tmp_path, content):
    path = tmp_path / "journal.json"
    path.write_text(content)
    read = jr.read_journal(path)
    assert read.state == "malformed" and read.entry is None and read.problem


def test_invalid_utf8_is_malformed(tmp_path):
    path = tmp_path / "journal.json"
    path.write_bytes(b"\xff\xfe\x00bad")
    assert jr.read_journal(path).state == "malformed"


def test_oversized_file_is_malformed(tmp_path):
    path = tmp_path / "journal.json"
    path.write_text(" " * (jr._MAX_JOURNAL_BYTES + 1))
    assert jr.read_journal(path).state == "malformed"


def test_partial_journal_is_incomplete(tmp_path):
    path = tmp_path / "journal.json"
    path.write_text(json.dumps({"schema_version": 1, "operation": "update"}))
    read = jr.read_journal(path)
    assert read.state == "incomplete" and read.entry is None
    assert "phase" in read.problem


@pytest.mark.parametrize(
    "patch",
    [
        {"extras": "rag"},
        {"extras": [1]},
        {"operation": "purge"},
        {"phase": "done"},
        {"started_at": 5},
        {"from_version": 9},
        {"installer": None},
    ],
)
def test_wrong_types_or_values_are_malformed(tmp_path, patch):
    path = tmp_path / "journal.json"
    data = entry().to_json_dict() | patch
    path.write_text(json.dumps(data))
    assert jr.read_journal(path).state == "malformed"


def test_unknown_extra_fields_are_ignored(tmp_path):
    path = tmp_path / "journal.json"
    path.write_text(json.dumps(entry().to_json_dict() | {"future_field": 1}))
    assert jr.read_journal(path).state == "ok"


def test_no_secrets_are_persisted(tmp_path):
    secret = "sk-ant-api03-" + "Z" * 40
    bad = jr.JournalEntry(
        operation="update",
        phase="planned",
        started_at="t",
        from_version="1",
        specifier=f"=={secret}",
        extras=(),
    )
    path = tmp_path / "journal.json"
    with pytest.raises(ValueError, match="credential"):
        jr.write_journal(bad, path)
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_receipt_options_never_reach_the_journal(tmp_path):
    """uv's receipt can hold index/find-links URLs that embed credentials; they are not modelled."""
    fields = set(entry().to_json_dict())
    assert not {"options", "find_links", "index_url", "env"} & fields


def test_entries_require_confirmed_ownership():
    with pytest.raises(ValueError):
        jr.new_entry("update", confirmed(kind=OwnershipKind.AMBIGUOUS), python_version="3.12.4")
    with pytest.raises(ValueError):
        jr.new_entry("purge", confirmed(), python_version="3.12.4")


def test_default_path_follows_the_data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("VELUNE_DATA_HOME", str(tmp_path))
    assert jr.default_journal_path() == tmp_path / "lifecycle" / "journal.json"
    assert not (tmp_path / "lifecycle").exists()  # computing the path creates nothing


def test_failed_write_leaves_no_temp_file(tmp_path, monkeypatch):
    def boom(_fd):
        raise OSError("disk full")

    monkeypatch.setattr(jr.os, "fsync", boom)
    with pytest.raises(OSError):
        jr.write_journal(entry(), tmp_path / "journal.json")
    assert list(tmp_path.iterdir()) == []
