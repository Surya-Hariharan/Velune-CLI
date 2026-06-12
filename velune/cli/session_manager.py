"""Backward-compatible wrappers around :mod:`velune.cli.sessions`.

New code should use :class:`velune.cli.sessions.SessionStore` directly; these
functions keep the original flat API (and on-disk location) working.
"""

from __future__ import annotations

from pathlib import Path

from velune.cli.sessions import SessionStore

SESSIONS_DIR = Path.home() / ".velune" / "sessions"

_store = SessionStore(SESSIONS_DIR)


def save_session(conversation: list[dict], model_id: str, workspace: str) -> str:
    meta = _store.save(conversation, workspace=workspace, model_id=model_id)
    return meta.id


def list_sessions() -> list[dict]:
    return [
        {
            "id": m.id,
            "timestamp": m.created_at,
            "model_id": m.model_id,
            "turns": m.turn_count,
            "workspace": m.workspace,
        }
        for m in _store.list(limit=20)
    ]


def load_session(session_id: str) -> list[dict] | None:
    loaded = _store.load(session_id)
    return loaded[1] if loaded else None


def export_session_markdown(session_id: str) -> str | None:
    return _store.export_markdown(session_id)
