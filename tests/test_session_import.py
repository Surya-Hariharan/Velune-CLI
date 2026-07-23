"""Single-conversation import — distinct from velune backup/restore's
whole-store archive. `SessionStore.import_session` takes one already-
extracted `{"meta": {...}, "conversation": [...]}` payload (the shape
`save()` writes, and what a file under ~/.velune/sessions/ or inside a
backup archive's sessions/ folder looks like) and adds it as a new local
session.
"""

from __future__ import annotations

import json

import pytest

from velune.cli.sessions import SessionStore

_CONV = [
    {"role": "user", "content": "fix the auth bug"},
    {"role": "assistant", "content": "done"},
]


def _store(tmp_path) -> SessionStore:
    return SessionStore(root=tmp_path / "sessions")


def test_import_native_shape_creates_new_session(tmp_path):
    store = _store(tmp_path)
    payload = {
        "meta": {
            "id": "orig123",
            "title": "Auth bug fix",
            "model_id": "claude-x",
            "mode": "normal",
            "tags": ["auth"],
            "total_tokens": 42,
        },
        "conversation": _CONV,
    }
    meta = store.import_session(payload, workspace=str(tmp_path))

    assert meta.title == "Auth bug fix"
    assert meta.model_id == "claude-x"
    assert meta.tags == ["auth"]
    assert meta.total_tokens == 42
    assert meta.turn_count == 2
    assert meta.workspace == str(tmp_path)

    loaded = store.load(meta.id)
    assert loaded is not None
    assert loaded[1] == _CONV


def test_import_reuses_source_id_when_no_collision(tmp_path):
    store = _store(tmp_path)
    payload = {"meta": {"id": "orig123"}, "conversation": _CONV}
    meta = store.import_session(payload, workspace=str(tmp_path))
    assert meta.id == "orig123"


def test_import_mints_fresh_id_on_collision(tmp_path):
    store = _store(tmp_path)
    store.save(_CONV, workspace=str(tmp_path), model_id="m", session_id="orig123")

    payload = {"meta": {"id": "orig123", "title": "Imported copy"}, "conversation": _CONV}
    meta = store.import_session(payload, workspace=str(tmp_path))

    assert meta.id != "orig123"
    # The pre-existing local session is untouched.
    existing = store.load_meta("orig123")
    assert existing is not None
    assert existing.title != "Imported copy"


def test_import_retags_to_current_workspace(tmp_path):
    store = _store(tmp_path)
    payload = {
        "meta": {"id": "orig123", "workspace": "/some/other/machine/project"},
        "conversation": _CONV,
    }
    meta = store.import_session(payload, workspace=str(tmp_path))
    assert meta.workspace == str(tmp_path)


def test_import_flat_json_shape_falls_back_to_defaults(tmp_path):
    """`velune session show --json` emits a flat dict with no "meta" key —
    conversation still imports, with sensible metadata defaults."""
    store = _store(tmp_path)
    payload = {"id": "flat1", "conversation": _CONV}
    meta = store.import_session(payload, workspace=str(tmp_path))
    assert meta.turn_count == 2
    assert meta.title  # auto_title() fallback, not empty


def test_import_rejects_payload_without_conversation(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.import_session({"meta": {}}, workspace=str(tmp_path))


def test_import_round_trips_a_real_saved_file(tmp_path):
    """A file this same store previously wrote via save() must be importable
    as-is (the realistic "copied from ~/.velune/sessions/" scenario)."""
    store = _store(tmp_path)
    saved_meta = store.save(_CONV, workspace=str(tmp_path), model_id="m", session_id="s1")
    raw = (store.root / "s1.json").read_text(encoding="utf-8")
    data = json.loads(raw)

    other_store = SessionStore(root=tmp_path / "other-machine-sessions")
    imported_meta = other_store.import_session(data, workspace=str(tmp_path / "new-workspace"))

    assert imported_meta.id == saved_meta.id  # no collision in the fresh store
    assert other_store.load(imported_meta.id)[1] == _CONV
