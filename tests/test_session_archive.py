"""Session archive lifecycle tests.

The Session pillar requires an *Archive* verb: a non-destructive way to move a
finished session out of the default listing without deleting its conversation.
These tests pin the store-level contract that ``velune session archive`` /
``unarchive`` / ``list --archived`` build on.
"""

from __future__ import annotations

from velune.cli.sessions import SessionStore

_CONV = [
    {"role": "user", "content": "fix the auth bug"},
    {"role": "assistant", "content": "done"},
]


def _store(tmp_path) -> SessionStore:
    return SessionStore(root=tmp_path / "sessions")


def test_new_sessions_are_not_archived(tmp_path):
    store = _store(tmp_path)
    meta = store.save(_CONV, workspace=str(tmp_path), model_id="m", session_id="s1")
    assert meta.archived is False
    assert [m.id for m in store.list()] == ["s1"]


def test_archive_hides_from_default_list(tmp_path):
    store = _store(tmp_path)
    store.save(_CONV, workspace=str(tmp_path), model_id="m", session_id="s1")

    assert store.set_archived("s1", True) is True

    # Default listing hides it; archived_only surfaces it.
    assert store.list() == []
    archived = store.list(archived_only=True)
    assert [m.id for m in archived] == ["s1"]
    assert archived[0].archived is True
    # include_archived shows everything.
    assert [m.id for m in store.list(include_archived=True)] == ["s1"]


def test_unarchive_restores_to_default_list(tmp_path):
    store = _store(tmp_path)
    store.save(_CONV, workspace=str(tmp_path), model_id="m", session_id="s1")
    store.set_archived("s1", True)

    assert store.set_archived("s1", False) is True
    assert [m.id for m in store.list()] == ["s1"]
    assert store.list(archived_only=True) == []


def test_archived_flag_survives_resave(tmp_path):
    """Re-saving an archived session (e.g. after resuming it) preserves state."""
    store = _store(tmp_path)
    store.save(_CONV, workspace=str(tmp_path), model_id="m", session_id="s1")
    store.set_archived("s1", True)

    # Resume path re-saves under the same id — archived must not silently reset.
    reloaded = store.save(_CONV, workspace=str(tmp_path), model_id="m", session_id="s1")
    assert reloaded.archived is True


def test_set_archived_missing_session_returns_false(tmp_path):
    store = _store(tmp_path)
    assert store.set_archived("nope", True) is False


def test_conversation_preserved_across_archive(tmp_path):
    store = _store(tmp_path)
    store.save(_CONV, workspace=str(tmp_path), model_id="m", session_id="s1")
    store.set_archived("s1", True)

    loaded = store.load("s1")
    assert loaded is not None
    _, conversation = loaded
    assert conversation == _CONV
