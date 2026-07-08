"""Project-file auto-discovery for resources.

Complements each connector's live :meth:`~velune.resources.base.ResourceConnector.discover`
(which probes the running environment) by scanning the *workspace* for
configuration that reveals a resource the user likely wants to connect:

    docker-compose.yml / compose.yaml / compose.yml
    supabase/                     (a Supabase project directory)
    .env  →  DATABASE_URL, POSTGRES_URL, MYSQL_URL,
             SUPABASE_URL, SUPABASE_ANON_KEY

Everything here is read-only and defensive: unreadable or malformed files yield
no hints rather than an error, and connection URLs are parsed for host/port/db
only — passwords in a ``DATABASE_URL`` are never copied into a hint.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse

from velune.resources.base import DiscoveryHint

logger = logging.getLogger("velune.resources.discovery")

_COMPOSE_FILES = ("docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml")

# .env keys → the connector that consumes them.
_ENV_DB_KEYS = {
    "DATABASE_URL": ("postgres", "PostgreSQL"),
    "POSTGRES_URL": ("postgres", "PostgreSQL"),
    "POSTGRESQL_URL": ("postgres", "PostgreSQL"),
    "MYSQL_URL": ("mysql", "MySQL"),
    "DATABASE_URL_MYSQL": ("mysql", "MySQL"),
}


def _parse_dotenv(path: Path) -> dict[str, str]:
    """Parse a ``.env`` file into a flat dict. Tolerant of comments/blank lines."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug("Could not read %s: %s", path, exc)
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        val = raw.strip().strip('"').strip("'")
        if key:
            values[key] = val
    return values


def _hint_from_db_url(
    resource_id: str, display: str, url: str, source: str
) -> DiscoveryHint | None:
    """Build a secret-free hint from a database URL. Password is dropped."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = parsed.hostname or "localhost"
    port = parsed.port
    database = parsed.path.lstrip("/") or None
    suggested: dict[str, object] = {"host": host}
    if port:
        suggested["port"] = port
    if database:
        suggested["database"] = database
    if parsed.username:
        suggested["username"] = parsed.username  # username is not a secret; password is dropped
    detail = f"{host}:{port}" if port else host
    return DiscoveryHint(
        resource_id=resource_id,
        display_name=display,
        detail=detail,
        source=source,
        suggested=suggested,
    )


def scan_workspace(workspace: Path | None) -> list[DiscoveryHint]:
    """Scan *workspace* for resource configuration and return discovery hints.

    Never raises; a missing or unreadable workspace yields an empty list.
    """
    if workspace is None:
        return []
    root = Path(workspace)
    if not root.is_dir():
        return []

    hints: list[DiscoveryHint] = []

    # ── Docker Compose ────────────────────────────────────────────────────
    for fname in _COMPOSE_FILES:
        fpath = root / fname
        if fpath.is_file():
            hints.append(
                DiscoveryHint(
                    resource_id="docker",
                    display_name="Docker Compose project",
                    detail=fname,
                    source=fname,
                )
            )
            break  # one compose hint is enough

    # ── Supabase project directory ────────────────────────────────────────
    supabase_dir = root / "supabase"
    if supabase_dir.is_dir():
        hints.append(
            DiscoveryHint(
                resource_id="supabase",
                display_name="Supabase project",
                detail="supabase/",
                source="supabase/",
            )
        )

    # ── .env parsing ──────────────────────────────────────────────────────
    env_path = root / ".env"
    if env_path.is_file():
        env = _parse_dotenv(env_path)
        seen_ids: set[str] = {h.resource_id for h in hints}

        for key, (rid, display) in _ENV_DB_KEYS.items():
            if key in env and env[key]:
                hint = _hint_from_db_url(rid, display, env[key], ".env")
                if hint and rid not in seen_ids:
                    hints.append(hint)
                    seen_ids.add(rid)

        if env.get("SUPABASE_URL"):
            if "supabase" not in seen_ids:
                url = env["SUPABASE_URL"]
                host = ""
                try:
                    host = urlparse(url).hostname or ""
                except ValueError:
                    host = ""
                hints.append(
                    DiscoveryHint(
                        resource_id="supabase",
                        display_name="Supabase project",
                        detail=host or "configured in .env",
                        source=".env",
                        # URL is not secret; the anon/service key is never copied.
                        suggested={"url": url},
                    )
                )
                seen_ids.add("supabase")

    return hints
