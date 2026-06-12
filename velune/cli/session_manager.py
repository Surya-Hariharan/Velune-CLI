"""Persist and restore REPL conversation sessions to ~/.velune/sessions/."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

SESSIONS_DIR = Path.home() / ".velune" / "sessions"


def save_session(conversation: list[dict], model_id: str, workspace: str) -> str:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    session_id = uuid.uuid4().hex[:8]
    data = {
        "id": session_id,
        "timestamp": datetime.now().isoformat(),
        "model_id": model_id,
        "workspace": workspace,
        "conversation": conversation,
        "turn_count": len(conversation),
    }
    (SESSIONS_DIR / f"{session_id}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return session_id


def list_sessions() -> list[dict]:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    sessions = []
    for f in sorted(SESSIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            sessions.append(
                {
                    "id": data["id"],
                    "timestamp": data["timestamp"],
                    "model_id": data.get("model_id", "unknown"),
                    "turns": data.get("turn_count", 0),
                    "workspace": data.get("workspace", ""),
                }
            )
        except Exception:
            pass
    return sessions[:20]


def load_session(session_id: str) -> list[dict] | None:
    path = SESSIONS_DIR / f"{session_id}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("conversation", [])


def export_session_markdown(session_id: str) -> str | None:
    path = SESSIONS_DIR / f"{session_id}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    lines = [
        f"# Velune Session — {data['timestamp']}",
        f"**Model:** {data.get('model_id', 'unknown')}",
        f"**Workspace:** {data.get('workspace', 'unknown')}",
        "",
    ]
    for turn in data.get("conversation", []):
        lines.append(f"### {turn['role'].capitalize()}")
        lines.append(turn["content"])
        lines.append("")
    return "\n".join(lines)
