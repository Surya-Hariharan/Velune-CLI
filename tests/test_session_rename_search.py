"""SessionStore.rename() and .search_content() — "/session rename" and
"/session search" build on these store-level contracts.
"""

from __future__ import annotations

import pytest

from velune.cli.sessions import SessionStore

_AUTH_CONV = [
    {"role": "user", "content": "fix the auth bug in login.py"},
    {"role": "assistant", "content": "Found it — the token was never refreshed."},
]
_DB_CONV = [
    {"role": "user", "content": "why is the database connection pool leaking?"},
    {"role": "assistant", "content": "You weren't closing connections on error."},
]


def _store(tmp_path) -> SessionStore:
    return SessionStore(root=tmp_path / "sessions")


# ---------------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------------


def test_rename_updates_title(tmp_path):
    store = _store(tmp_path)
    store.save(_AUTH_CONV, workspace=str(tmp_path), model_id="m", session_id="s1")

    updated = store.rename("s1", "Auth token refresh fix")

    assert updated is not None
    assert updated.title == "Auth token refresh fix"
    assert store.load_meta("s1").title == "Auth token refresh fix"


def test_rename_preserves_conversation_and_other_meta(tmp_path):
    store = _store(tmp_path)
    store.save(_AUTH_CONV, workspace=str(tmp_path), model_id="claude-x", session_id="s1")

    store.rename("s1", "New title")

    loaded = store.load("s1")
    assert loaded is not None
    meta, conversation = loaded
    assert conversation == _AUTH_CONV
    assert meta.model_id == "claude-x"


def test_rename_missing_session_returns_none(tmp_path):
    store = _store(tmp_path)
    assert store.rename("nope", "New title") is None


def test_rename_rejects_empty_title(tmp_path):
    store = _store(tmp_path)
    store.save(_AUTH_CONV, workspace=str(tmp_path), model_id="m", session_id="s1")
    with pytest.raises(ValueError):
        store.rename("s1", "   ")


def test_rename_strips_whitespace(tmp_path):
    store = _store(tmp_path)
    store.save(_AUTH_CONV, workspace=str(tmp_path), model_id="m", session_id="s1")
    updated = store.rename("s1", "  Trimmed title  ")
    assert updated.title == "Trimmed title"


# ---------------------------------------------------------------------------
# search_content
# ---------------------------------------------------------------------------


def test_search_finds_matching_turn_content(tmp_path):
    store = _store(tmp_path)
    store.save(_AUTH_CONV, workspace=str(tmp_path), model_id="m", session_id="auth")
    store.save(_DB_CONV, workspace=str(tmp_path), model_id="m", session_id="db")

    hits = store.search_content("token", workspace=str(tmp_path))

    assert [h.meta.id for h in hits] == ["auth"]
    assert hits[0].match_count >= 1
    assert any("token" in s for s in hits[0].snippets)


def test_search_is_case_insensitive(tmp_path):
    store = _store(tmp_path)
    store.save(_AUTH_CONV, workspace=str(tmp_path), model_id="m", session_id="auth")

    hits = store.search_content("TOKEN", workspace=str(tmp_path))
    assert len(hits) == 1


def test_search_matches_title_too(tmp_path):
    store = _store(tmp_path)
    store.save(
        _DB_CONV,
        workspace=str(tmp_path),
        model_id="m",
        session_id="db",
        title="Connection pool leak",
    )

    hits = store.search_content("connection pool", workspace=str(tmp_path))
    assert [h.meta.id for h in hits] == ["db"]


def test_search_no_matches_returns_empty(tmp_path):
    store = _store(tmp_path)
    store.save(_AUTH_CONV, workspace=str(tmp_path), model_id="m", session_id="auth")
    assert store.search_content("nonexistent phrase", workspace=str(tmp_path)) == []


def test_search_excludes_archived_by_default(tmp_path):
    store = _store(tmp_path)
    store.save(_AUTH_CONV, workspace=str(tmp_path), model_id="m", session_id="auth")
    store.set_archived("auth", True)

    assert store.search_content("token", workspace=str(tmp_path)) == []
    included = store.search_content("token", workspace=str(tmp_path), include_archived=True)
    assert [h.meta.id for h in included] == ["auth"]


def test_search_scoped_to_workspace(tmp_path):
    store = _store(tmp_path)
    other_ws = tmp_path / "other"
    store.save(_AUTH_CONV, workspace=str(tmp_path), model_id="m", session_id="auth")
    store.save(_AUTH_CONV, workspace=str(other_ws), model_id="m", session_id="auth-other")

    hits = store.search_content("token", workspace=str(tmp_path))
    assert [h.meta.id for h in hits] == ["auth"]


def test_search_across_all_workspaces_when_unscoped(tmp_path):
    store = _store(tmp_path)
    other_ws = tmp_path / "other"
    store.save(_AUTH_CONV, workspace=str(tmp_path), model_id="m", session_id="auth")
    store.save(_AUTH_CONV, workspace=str(other_ws), model_id="m", session_id="auth-other")

    hits = store.search_content("token", workspace=None)
    assert {h.meta.id for h in hits} == {"auth", "auth-other"}


def test_search_ranks_by_match_count():
    """More matching turns should sort first."""
    from velune.cli.sessions import SessionSearchHit

    metas = [
        SessionSearchHit(meta=_fake_meta("low"), match_count=1),
        SessionSearchHit(meta=_fake_meta("high"), match_count=5),
    ]
    metas.sort(key=lambda h: (h.match_count, h.meta.updated_at), reverse=True)
    assert metas[0].meta.id == "high"


def test_search_blank_query_returns_empty(tmp_path):
    store = _store(tmp_path)
    store.save(_AUTH_CONV, workspace=str(tmp_path), model_id="m", session_id="auth")
    assert store.search_content("   ", workspace=str(tmp_path)) == []


def _fake_meta(session_id: str):
    from velune.cli.sessions import SessionMeta

    return SessionMeta(
        id=session_id,
        title="t",
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        workspace="/ws",
        project_name="p",
        model_id="m",
    )
