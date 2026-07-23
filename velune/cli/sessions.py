"""Workspace-scoped conversation session store with metadata and auto-naming.

Sessions are the *conversation* layer of Velune's workspace model: each one is
an isolated rolling context that lives inside a persistent project workspace.
Archiving or switching sessions never touches project memory, embeddings, or
repository cognition — those belong to the workspace, not the conversation.

Snapshots are stored as one JSON file per session under
``~/.velune/sessions/`` with a ``meta`` block (title, project, model, mode,
tags, token usage) plus the full conversation, so any session can be resumed
exactly as it was left.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

_log = logging.getLogger("velune.cli.sessions")

DEFAULT_SESSIONS_DIR = Path.home() / ".velune" / "sessions"

# Words that carry no intent signal when deriving a session title.
_TITLE_STOPWORDS = frozenset(
    "a an the and or but if then else please can could would should you your "
    "i we me my our us it its this that these those of in on at to for with "
    "is are was were be been being do does did have has had how what why "
    "when where which who whom there here about into from as by want need "
    "help hey hi hello okay ok just some any let lets".split()
)


@dataclass(slots=True)
class SessionMeta:
    """Metadata describing one conversational session."""

    id: str
    title: str
    created_at: str
    updated_at: str
    workspace: str
    project_name: str
    model_id: str
    mode: str = "normal"
    tags: list[str] = field(default_factory=list)
    total_tokens: int = 0
    turn_count: int = 0
    summary: str | None = None
    archived: bool = False


@dataclass(slots=True)
class SessionSearchHit:
    """One session matched by :meth:`SessionStore.search_content`."""

    meta: SessionMeta
    match_count: int
    snippets: list[str] = field(default_factory=list)


def _excerpt(content: str, needle: str, *, radius: int = 60) -> str:
    """Short, single-line excerpt of *content* centered on *needle*."""
    flat = " ".join(content.split())
    idx = flat.lower().find(needle)
    if idx == -1:
        return flat[: radius * 2].rstrip()
    start = max(0, idx - radius)
    end = min(len(flat), idx + len(needle) + radius)
    excerpt = flat[start:end]
    if start > 0:
        excerpt = "…" + excerpt
    if end < len(flat):
        excerpt = excerpt + "…"
    return excerpt


def auto_title(conversation: list[dict], max_words: int = 6, max_chars: int = 48) -> str:
    """Derive a human-readable session title from the conversation's intent.

    Uses the first substantive user message: strips slash-command prefixes and
    filler words, then keeps the leading significant words in their original
    order so titles read like "JWT refresh debugging" rather than raw prompts.
    """
    first_user = next(
        (m.get("content", "") for m in conversation if m.get("role") == "user"),
        "",
    )
    text = first_user.strip()
    if text.startswith("/"):
        # "/run fix the auth bug" → "fix the auth bug"
        text = text.split(None, 1)[1] if " " in text else ""
    text = text.splitlines()[0] if text else ""
    words = re.findall(r"[A-Za-z0-9_./-]+", text)
    significant = [w for w in words if w.lower() not in _TITLE_STOPWORDS]
    chosen = (significant or words)[:max_words]
    if not chosen:
        return "Untitled session"
    title = " ".join(chosen)
    if len(title) > max_chars:
        title = title[: max_chars - 1].rstrip() + "…"
    return title[0].upper() + title[1:]


class SessionStore:
    """Persist, list, and restore conversation sessions, namespaced by workspace."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or DEFAULT_SESSIONS_DIR

    # ── Persistence ──────────────────────────────────────────────────────

    def save(
        self,
        conversation: list[dict],
        *,
        workspace: str,
        model_id: str,
        mode: str = "normal",
        title: str | None = None,
        tags: list[str] | None = None,
        total_tokens: int = 0,
        session_id: str | None = None,
        summary: str | None = None,
    ) -> SessionMeta:
        """Write a session snapshot; reuses *session_id* to update in place."""
        self.root.mkdir(parents=True, exist_ok=True)
        now = datetime.now().isoformat(timespec="seconds")
        sid = session_id or uuid.uuid4().hex[:8]
        created = now
        archived = False
        if session_id:
            existing = self.load_meta(session_id)
            if existing:
                created = existing.created_at
                # Re-saving an archived session (e.g. resuming it) must not
                # silently un-archive it; preserve the flag unless unarchived.
                archived = existing.archived
        meta = SessionMeta(
            id=sid,
            title=title or auto_title(conversation),
            created_at=created,
            updated_at=now,
            workspace=workspace,
            project_name=Path(workspace).name if workspace else "unknown",
            model_id=model_id,
            mode=mode,
            tags=tags or [],
            total_tokens=total_tokens,
            turn_count=len(conversation),
            summary=summary,
            archived=archived,
        )
        payload = {"meta": asdict(meta), "conversation": conversation}
        path = self.root / f"{sid}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
        return meta

    def load(self, session_id: str) -> tuple[SessionMeta, list[dict]] | None:
        data = self._read(session_id)
        if data is None:
            return None
        return self._meta_from(data), data.get("conversation", [])

    def load_meta(self, session_id: str) -> SessionMeta | None:
        data = self._read(session_id)
        return self._meta_from(data) if data is not None else None

    def list(
        self,
        workspace: str | None = None,
        limit: int = 50,
        *,
        include_archived: bool = False,
        archived_only: bool = False,
    ) -> list[SessionMeta]:
        """Sessions sorted newest-first, optionally filtered to one workspace.

        Archived sessions are hidden by default so the active list stays focused;
        pass ``include_archived=True`` to show everything or ``archived_only=True``
        to show just the archive.
        """
        if not self.root.exists():
            return []
        metas: list[SessionMeta] = []
        for f in self.root.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                meta = self._meta_from(data)
                if workspace is not None and not self._same_workspace(meta.workspace, workspace):
                    continue
                if archived_only and not meta.archived:
                    continue
                if not include_archived and not archived_only and meta.archived:
                    continue
                metas.append(meta)
            except Exception:
                continue
        metas.sort(key=lambda m: m.updated_at, reverse=True)
        return metas[:limit]

    def search_content(
        self,
        query: str,
        *,
        workspace: str | None = None,
        limit: int = 20,
        include_archived: bool = False,
        max_snippets: int = 3,
    ) -> list[SessionSearchHit]:
        """Case-insensitive substring search across every session's turns.

        Unlike ``list()`` (browsing by recency/title), this is "I said
        something about X a while back, which conversation was that" —
        searches actual turn *content*, not just the auto-generated title.
        Matching sessions are sorted by match count (most relevant first),
        each carrying up to *max_snippets* short excerpts so the caller can
        show why it matched without loading the full conversation.
        """
        if not self.root.exists() or not query.strip():
            return []
        needle = query.strip().lower()
        hits: list[SessionSearchHit] = []

        for f in self.root.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                meta = self._meta_from(data)
            except Exception:
                continue
            if workspace is not None and not self._same_workspace(meta.workspace, workspace):
                continue
            if meta.archived and not include_archived:
                continue

            snippets: list[str] = []
            match_count = 0
            if needle in meta.title.lower():
                match_count += 1
            for turn in data.get("conversation", []):
                content = turn.get("content") or ""
                if needle not in content.lower():
                    continue
                match_count += 1
                if len(snippets) < max_snippets:
                    snippets.append(_excerpt(content, needle))

            if match_count:
                hits.append(SessionSearchHit(meta=meta, match_count=match_count, snippets=snippets))

        hits.sort(key=lambda h: (h.match_count, h.meta.updated_at), reverse=True)
        return hits[:limit]

    def delete(self, session_id: str) -> bool:
        path = self.root / f"{session_id}.json"
        if not path.exists():
            return False
        path.unlink()
        return True

    def set_archived(self, session_id: str, archived: bool) -> bool:
        """Flip a session's archived flag in place. Returns False if not found.

        Archiving never touches the conversation, project memory, or embeddings —
        it only moves the session out of the default listing.
        """
        data = self._read(session_id)
        if data is None:
            return False
        meta = self._meta_from(data)
        meta.archived = archived
        payload = {"meta": asdict(meta), "conversation": data.get("conversation", [])}
        path = self.root / f"{session_id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
        return True

    def rename(self, session_id: str, new_title: str) -> SessionMeta | None:
        """Rename a session's title in place. Returns the updated meta, or
        None if the session doesn't exist.

        Like ``set_archived()``, this only touches the meta block — the
        conversation, project memory, and embeddings are untouched.
        """
        new_title = new_title.strip()
        if not new_title:
            raise ValueError("Session title cannot be empty.")
        data = self._read(session_id)
        if data is None:
            return None
        meta = self._meta_from(data)
        meta.title = new_title
        payload = {"meta": asdict(meta), "conversation": data.get("conversation", [])}
        path = self.root / f"{session_id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
        return meta

    def import_session(
        self,
        data: dict,
        *,
        workspace: str,
        session_id: str | None = None,
    ) -> SessionMeta:
        """Import a single conversation snapshot as a new local session.

        Distinct from ``velune backup``/``restore``, which snapshot and
        restore the *entire* store (every session, plus config/providers/
        memory/trust) as one archive. This takes one already-extracted
        conversation — the same ``{"meta": {...}, "conversation": [...]}``
        shape ``save()`` writes, e.g. a file copied from another machine's
        ``~/.velune/sessions/`` or pulled out of a backup archive's
        ``sessions/`` folder — and adds it here as a new session.

        Always re-tagged to *workspace*: an imported conversation is being
        brought INTO the current project, not restored onto the machine it
        came from. A source id that collides with an existing local session
        gets a fresh one minted instead, so importing can never silently
        overwrite something already here.
        """
        conversation = data.get("conversation")
        if not isinstance(conversation, list):
            raise ValueError("Import file has no 'conversation' array.")

        meta_raw = data.get("meta") or {}
        sid = session_id or meta_raw.get("id") or uuid.uuid4().hex[:8]
        if self.load_meta(sid) is not None:
            sid = uuid.uuid4().hex[:8]

        return self.save(
            conversation,
            workspace=workspace,
            model_id=meta_raw.get("model_id", "unknown"),
            mode=meta_raw.get("mode", "normal"),
            title=meta_raw.get("title"),
            tags=meta_raw.get("tags"),
            total_tokens=meta_raw.get("total_tokens", 0),
            session_id=sid,
            summary=meta_raw.get("summary"),
        )

    def export_markdown(self, session_id: str) -> str | None:
        loaded = self.load(session_id)
        if loaded is None:
            return None
        meta, conversation = loaded
        lines = [
            f"# Velune Session — {meta.title}",
            f"**Created:** {meta.created_at}",
            f"**Model:** {meta.model_id}",
            f"**Project:** {meta.project_name}",
            f"**Workspace:** {meta.workspace}",
            "",
        ]
        for turn in conversation:
            lines.append(f"### {turn.get('role', 'unknown').capitalize()}")
            lines.append(turn.get("content", ""))
            lines.append("")
        return "\n".join(lines)

    # ── Autosave / crash recovery ────────────────────────────────────────

    @property
    def autosave_dir(self) -> Path:
        """Sidecar directory holding the live, crash-guarded conversation."""
        return self.root / ".autosave"

    def autosave(
        self,
        conversation: list[dict],
        *,
        session_id: str,
        workspace: str,
        model_id: str,
        mode: str = "normal",
        total_tokens: int = 0,
    ) -> None:
        """Persist the active conversation to a crash-guard sidecar.

        A clean shutdown deletes this file via :meth:`clear_autosave`; anything
        left behind means the process exited unexpectedly, so it is treated as
        an orphan recoverable through ``velune recover``.
        """
        self.autosave_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now().isoformat(timespec="seconds")
        meta = SessionMeta(
            id=session_id,
            title=auto_title(conversation),
            created_at=now,
            updated_at=now,
            workspace=workspace,
            project_name=Path(workspace).name if workspace else "unknown",
            model_id=model_id,
            mode=mode,
            total_tokens=total_tokens,
            turn_count=len(conversation),
        )
        payload = {
            "meta": asdict(meta),
            "conversation": conversation,
            "crash_guard": True,
            "pid": os.getpid(),
            "host": socket.gethostname(),
        }
        path = self.autosave_dir / f"{session_id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)

    def clear_autosave(self, session_id: str) -> None:
        """Remove the crash-guard sidecar after a clean shutdown."""
        path = self.autosave_dir / f"{session_id}.json"
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except Exception as exc:  # pragma: no cover - best effort
            _log.debug("Could not clear autosave %s: %s", session_id, exc)

    def list_orphaned_autosaves(self, workspace: str | None = None) -> list[SessionMeta]:
        """Crash-guard sidecars left by sessions that did not exit cleanly.

        Filtered to *workspace* by default so a crash in one project never
        surfaces as a recoverable session while sitting in another project's
        directory; pass ``None`` explicitly to see orphans from every
        workspace (e.g. for a global ``velune recover --all-workspaces``).
        """
        if not self.autosave_dir.exists():
            return []
        metas: list[SessionMeta] = []
        for f in self.autosave_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                meta = self._meta_from(data)
                if workspace is not None and not self._same_workspace(meta.workspace, workspace):
                    continue
                metas.append(meta)
            except Exception:
                continue
        metas.sort(key=lambda m: m.updated_at, reverse=True)
        return metas

    def recover_autosave(self, session_id: str) -> SessionMeta | None:
        """Promote a crash-guard sidecar into a real session, then clear it.

        Returns the saved session meta, or None if no such sidecar exists.
        """
        path = self.autosave_dir / f"{session_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            _log.warning("Could not read autosave %s: %s", session_id, exc)
            return None
        meta = self._meta_from(data)
        conversation = data.get("conversation", [])
        saved = self.save(
            conversation,
            workspace=meta.workspace,
            model_id=meta.model_id,
            mode=meta.mode,
            title=meta.title,
            total_tokens=meta.total_tokens,
            session_id=session_id,
        )
        self.clear_autosave(session_id)
        return saved

    def discard_autosave(self, session_id: str) -> bool:
        """Delete a crash-guard sidecar without recovering it."""
        path = self.autosave_dir / f"{session_id}.json"
        if not path.exists():
            return False
        path.unlink()
        return True

    # ── Internals ────────────────────────────────────────────────────────

    def _read(self, session_id: str) -> dict | None:
        path = self.root / f"{session_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            _log.warning("Could not read session %s: %s", session_id, exc)
            return None

    @staticmethod
    def _same_workspace(a: str, b: str) -> bool:
        try:
            return Path(a).resolve() == Path(b).resolve()
        except Exception:
            return a == b

    @staticmethod
    def _meta_from(data: dict) -> SessionMeta:
        raw = data.get("meta")
        if raw:
            known = set(SessionMeta.__dataclass_fields__)
            return SessionMeta(**{k: v for k, v in raw.items() if k in known})
        # Legacy flat format from the original session_manager module.
        conversation = data.get("conversation", [])
        ts = data.get("timestamp", datetime.now().isoformat(timespec="seconds"))
        workspace = data.get("workspace", "")
        return SessionMeta(
            id=data.get("id", "unknown"),
            title=auto_title(conversation),
            created_at=ts,
            updated_at=ts,
            workspace=workspace,
            project_name=Path(workspace).name if workspace else "unknown",
            model_id=data.get("model_id", "unknown"),
            turn_count=data.get("turn_count", len(conversation)),
        )
