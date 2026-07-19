"""Provider-aware token and cost tracking for sessions.

Schema v2 adds:
- provider_id column for per-provider analytics
- latency_ms column for performance tracking
- success column and error_code for failure analysis
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

import structlog

logger = structlog.get_logger()


def _utcnow_naive() -> datetime:
    """Current UTC time as a *naive* datetime.

    Stored timestamps are compared lexically against these values, so the
    ISO format must stay identical to the historical ``datetime.utcnow()``
    output (no ``+00:00`` offset suffix). This is the deprecation-safe
    equivalent, not a behavior change.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Cost per 1M tokens (input, output) in USD — updated June 2026
PROVIDER_COSTS: dict[str, dict[str, dict[str, float]]] = {
    "anthropic": {
        "claude-opus-4-5": {"input": 15.0, "output": 75.0},
        "claude-opus-4-8": {"input": 15.0, "output": 75.0},
        "claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
        "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
        "claude-haiku-4-5": {"input": 0.80, "output": 4.0},
        "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.0},
    },
    "openai": {
        "gpt-4o": {"input": 5.0, "output": 15.0},
        "gpt-4o-mini": {"input": 0.15, "output": 0.60},
        "o1": {"input": 15.0, "output": 60.0},
        "o1-mini": {"input": 3.0, "output": 12.0},
        "gpt-4-turbo": {"input": 10.0, "output": 30.0},
        "gpt-3.5-turbo": {"input": 0.50, "output": 1.50},
    },
    "google": {
        "gemini-2.0-flash": {"input": 0.075, "output": 0.30},
        "gemini-2.0-flash-exp": {"input": 0.0, "output": 0.0},
        "gemini-1.5-pro": {"input": 3.50, "output": 10.50},
        "gemini-1.5-flash": {"input": 0.075, "output": 0.30},
        "gemini-2.5-pro": {"input": 1.25, "output": 10.0},
    },
    "groq": {
        # Groq free tier — $0 for public models. mixtral-8x7b-32768,
        # gemma2-9b-it, and llama-3.2-11b-vision-preview were decommissioned
        # by Groq (2026-07) and removed — see providers/adapters/groq.py.
        "llama-3.3-70b-versatile": {"input": 0.0, "output": 0.0},
        "llama-3.1-8b-instant": {"input": 0.0, "output": 0.0},
        "openai/gpt-oss-120b": {"input": 0.0, "output": 0.0},
        "qwen/qwen3-32b": {"input": 0.0, "output": 0.0},
    },
    "xai": {
        "grok-2": {"input": 2.0, "output": 10.0},
        "grok-2-mini": {"input": 0.20, "output": 1.0},
        "grok-beta": {"input": 5.0, "output": 15.0},
    },
    "deepseek": {
        "deepseek-chat": {"input": 0.14, "output": 0.28},
        "deepseek-reasoner": {"input": 0.55, "output": 2.19},
    },
    "mistral": {
        "mistral-large-latest": {"input": 2.0, "output": 6.0},
        "mistral-small-latest": {"input": 0.20, "output": 0.60},
        "codestral-latest": {"input": 0.30, "output": 0.90},
        "mistral-nemo": {"input": 0.15, "output": 0.15},
    },
    "cohere": {
        "command-r-plus-08-2024": {"input": 2.50, "output": 10.0},
        "command-r-08-2024": {"input": 0.15, "output": 0.60},
    },
    "nvidia": {
        "meta/llama-3.3-70b-instruct": {"input": 0.27, "output": 0.27},
        "mistralai/mistral-large-2-instruct": {"input": 2.0, "output": 6.0},
        "nvidia/llama-3.1-nemotron-70b-instruct": {"input": 0.35, "output": 0.35},
    },
    "together": {
        "meta-llama/Llama-3.3-70B-Instruct-Turbo": {"input": 0.88, "output": 0.88},
        "mistralai/Mistral-7B-Instruct-v0.3": {"input": 0.20, "output": 0.20},
        "Qwen/Qwen2.5-72B-Instruct-Turbo": {"input": 1.20, "output": 1.20},
    },
    "fireworks": {
        "accounts/fireworks/models/llama-v3p3-70b-instruct": {"input": 0.90, "output": 0.90},
        "accounts/fireworks/models/mixtral-8x22b-instruct": {"input": 1.20, "output": 1.20},
    },
    "openrouter": {},  # Dynamic — populated from model metadata at runtime
    "meta": {},  # Preview pricing not yet finalized publicly — treat as unknown, not free
    "zai": {
        "glm-4.5-air": {"input": 0.0, "output": 0.0},  # free tier
    },
    "ollama": {},  # Local — always free
    "lmstudio": {},  # Local — always free
    "huggingface": {},  # Inference API — complex pricing, treat as $0 for tracking
}

_FALLBACK_COSTS: dict[str, dict[str, float]] = {
    # Fallback prefix matching when exact model ID is not in the table
    "claude-opus": {"input": 15.0, "output": 75.0},
    "claude-sonnet": {"input": 3.0, "output": 15.0},
    "claude-haiku": {"input": 0.80, "output": 4.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 5.0, "output": 15.0},
    "gpt-4": {"input": 10.0, "output": 30.0},
    "gpt-3.5": {"input": 0.50, "output": 1.50},
    "gemini-2.0": {"input": 0.075, "output": 0.30},
    "gemini-1.5": {"input": 3.50, "output": 10.50},
    "deepseek": {"input": 0.14, "output": 0.28},
    "mistral": {"input": 0.20, "output": 0.60},
    "llama": {"input": 0.27, "output": 0.27},
}


@dataclass
class UsageEvent:
    """A single model completion event for the analytics pipeline."""

    provider_id: str
    model_id: str
    session_id: str
    input_tokens: int
    output_tokens: int
    latency_ms: float = 0.0
    success: bool = True
    error_code: str | None = None
    cost_usd: float | None = None
    timestamp: str = field(default_factory=lambda: _utcnow_naive().isoformat())

    def __post_init__(self) -> None:
        if self.cost_usd is None:
            self.cost_usd = _estimate_cost(
                self.provider_id, self.model_id, self.input_tokens, self.output_tokens
            )


@dataclass
class UsageRecord:
    """Single record of model usage (DB row)."""

    session_id: str
    timestamp: str
    provider_id: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    latency_ms: float = 0.0
    success: bool = True
    error_code: str | None = None
    estimated_cost: float | None = None


@dataclass
class ProviderUsage:
    """Aggregate usage statistics for a single provider."""

    provider_id: str
    requests: int = 0
    successes: int = 0
    failures: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    avg_latency_ms: float = 0.0
    models_used: list[str] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return (self.successes / self.requests * 100) if self.requests else 0.0


@dataclass
class ModelUsage:
    """Aggregate usage statistics for a single model."""

    model_id: str
    provider_id: str
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    avg_latency_ms: float = 0.0


@dataclass
class SessionUsage:
    """Summary of usage for a session."""

    session_id: str
    total_tokens: int
    total_cost: float | None
    total_input_tokens: int
    total_output_tokens: int
    model_breakdown: dict[str, int]
    provider_breakdown: dict[str, int]
    record_count: int
    start_time: str
    end_time: str


@dataclass
class UsageSummary:
    """Alias kept for backwards compatibility."""

    session_id: str
    total_tokens: int
    total_cost: float | None
    total_input_tokens: int
    total_output_tokens: int
    model_breakdown: dict[str, int]
    record_count: int
    start_time: str
    end_time: str


def _estimate_cost(
    provider_id: str, model_id: str, input_tokens: int, output_tokens: int
) -> float | None:
    """Return estimated USD cost or None for free/unknown providers."""
    if provider_id in ("ollama", "lmstudio", "llamacpp"):
        return 0.0

    # Exact match first
    provider_table = PROVIDER_COSTS.get(provider_id, {})
    rates = provider_table.get(model_id)

    # Prefix match fallback
    if rates is None:
        model_lower = model_id.lower()
        for prefix, r in _FALLBACK_COSTS.items():
            if prefix.lower() in model_lower:
                rates = r
                break

    if rates is None:
        return None

    input_cost = (input_tokens / 1_000_000) * rates["input"]
    output_cost = (output_tokens / 1_000_000) * rates["output"]
    return input_cost + output_cost


class SessionUsageTracker:
    """Tracks token and cost usage per session with full provider attribution."""

    _SCHEMA_VERSION = 3

    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is None:
            db_path = Path.home() / ".velune" / "telemetry" / "usage.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._lock = Lock()
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            # Main records table — v2 schema with provider_id, latency, success columns
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_records (
                    id          INTEGER PRIMARY KEY,
                    session_id  TEXT    NOT NULL,
                    timestamp   TEXT    NOT NULL,
                    provider_id TEXT    NOT NULL DEFAULT '',
                    model       TEXT    NOT NULL,
                    input_tokens  INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens  INTEGER NOT NULL DEFAULT 0,
                    latency_ms    REAL    NOT NULL DEFAULT 0,
                    success       INTEGER NOT NULL DEFAULT 1,
                    error_code    TEXT,
                    estimated_cost REAL,
                    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # Migrate v1/v2 → v3: add missing columns if absent
            existing = {row[1] for row in conn.execute("PRAGMA table_info(usage_records)")}
            for col, defn in [
                ("provider_id", "TEXT NOT NULL DEFAULT ''"),
                ("latency_ms", "REAL NOT NULL DEFAULT 0"),
                ("success", "INTEGER NOT NULL DEFAULT 1"),
                ("error_code", "TEXT"),
                ("cache_creation_tokens", "INTEGER NOT NULL DEFAULT 0"),
                ("cache_read_tokens", "INTEGER NOT NULL DEFAULT 0"),
            ]:
                if col not in existing:
                    conn.execute(f"ALTER TABLE usage_records ADD COLUMN {col} {defn}")

            conn.execute("CREATE INDEX IF NOT EXISTS idx_session_id ON usage_records(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON usage_records(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_provider ON usage_records(provider_id)")
            conn.commit()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def record_event(self, event: UsageEvent) -> None:
        """Record a :class:`UsageEvent` from the analytics pipeline."""
        total = event.input_tokens + event.output_tokens
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO usage_records
                    (session_id, timestamp, provider_id, model,
                     input_tokens, output_tokens, total_tokens,
                     latency_ms, success, error_code, estimated_cost,
                     cache_creation_tokens, cache_read_tokens)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.session_id,
                        event.timestamp,
                        event.provider_id,
                        event.model_id,
                        event.input_tokens,
                        event.output_tokens,
                        total,
                        event.latency_ms,
                        1 if event.success else 0,
                        event.error_code,
                        event.cost_usd,
                        getattr(event, "cache_creation_tokens", 0),
                        getattr(event, "cache_read_tokens", 0),
                    ),
                )
                conn.commit()
        logger.debug(
            "Usage event recorded",
            provider=event.provider_id,
            model=event.model_id,
            tokens=total,
            cost=event.cost_usd,
        )

    def record_completion(
        self,
        session_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        provider_id: str = "",
        latency_ms: float = 0.0,
        success: bool = True,
        error_code: str | None = None,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> None:
        """Record a model completion (backwards-compatible API)."""
        total_tokens = input_tokens + output_tokens
        estimated_cost = _estimate_cost(provider_id, model, input_tokens, output_tokens)

        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO usage_records
                    (session_id, timestamp, provider_id, model,
                     input_tokens, output_tokens, total_tokens,
                     latency_ms, success, error_code, estimated_cost,
                     cache_creation_tokens, cache_read_tokens)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        _utcnow_naive().isoformat(),
                        provider_id,
                        model,
                        input_tokens,
                        output_tokens,
                        total_tokens,
                        latency_ms,
                        1 if success else 0,
                        error_code,
                        estimated_cost,
                        cache_creation_tokens,
                        cache_read_tokens,
                    ),
                )
                conn.commit()

    # ------------------------------------------------------------------
    # Session queries
    # ------------------------------------------------------------------

    def get_session_total_tokens(self, session_id: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT SUM(total_tokens) FROM usage_records WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return row[0] if row and row[0] else 0

    def get_session_estimated_cost(self, session_id: str) -> float | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT SUM(estimated_cost) FROM usage_records WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return row[0] if row and row[0] else None

    def get_session_summary(self, session_id: str) -> UsageSummary | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT SUM(total_tokens), SUM(estimated_cost),
                       SUM(input_tokens), SUM(output_tokens),
                       COUNT(*), MIN(timestamp), MAX(timestamp)
                FROM usage_records
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()

            if not row or row[0] is None:
                return None

            model_rows = conn.execute(
                """
                SELECT model, SUM(total_tokens)
                FROM usage_records WHERE session_id = ?
                GROUP BY model
                """,
                (session_id,),
            ).fetchall()

        return UsageSummary(
            session_id=session_id,
            total_tokens=row[0],
            total_cost=row[1],
            total_input_tokens=row[2],
            total_output_tokens=row[3],
            model_breakdown={r[0]: r[1] for r in model_rows},
            record_count=row[4],
            start_time=row[5],
            end_time=row[6],
        )

    # ------------------------------------------------------------------
    # Provider-level analytics
    # ------------------------------------------------------------------

    def get_provider_usage(self, days: int = 30) -> list[ProviderUsage]:
        """Return per-provider usage aggregated over the last *days* days."""
        cutoff = (_utcnow_naive() - timedelta(days=days)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT
                    provider_id,
                    COUNT(*)                    AS requests,
                    SUM(success)                AS successes,
                    SUM(1 - success)            AS failures,
                    SUM(input_tokens)           AS input_tokens,
                    SUM(output_tokens)          AS output_tokens,
                    SUM(total_tokens)           AS total_tokens,
                    SUM(estimated_cost)         AS cost_usd,
                    AVG(latency_ms)             AS avg_latency_ms
                FROM usage_records
                WHERE timestamp > ?
                GROUP BY provider_id
                ORDER BY SUM(total_tokens) DESC
                """,
                (cutoff,),
            ).fetchall()

            result: list[ProviderUsage] = []
            for row in rows:
                pid = row[0] or "unknown"
                # Fetch distinct models used
                model_rows = conn.execute(
                    """
                    SELECT DISTINCT model FROM usage_records
                    WHERE provider_id = ? AND timestamp > ?
                    """,
                    (pid, cutoff),
                ).fetchall()
                result.append(
                    ProviderUsage(
                        provider_id=pid,
                        requests=row[1] or 0,
                        successes=row[2] or 0,
                        failures=row[3] or 0,
                        input_tokens=row[4] or 0,
                        output_tokens=row[5] or 0,
                        total_tokens=row[6] or 0,
                        cost_usd=row[7] or 0.0,
                        avg_latency_ms=row[8] or 0.0,
                        models_used=[r[0] for r in model_rows],
                    )
                )
        return result

    def get_model_usage(self, days: int = 30) -> list[ModelUsage]:
        """Return per-model usage aggregated over the last *days* days."""
        cutoff = (_utcnow_naive() - timedelta(days=days)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT
                    model,
                    provider_id,
                    COUNT(*)                    AS requests,
                    SUM(input_tokens)           AS input_tokens,
                    SUM(output_tokens)          AS output_tokens,
                    SUM(total_tokens)           AS total_tokens,
                    SUM(estimated_cost)         AS cost_usd,
                    AVG(latency_ms)             AS avg_latency_ms
                FROM usage_records
                WHERE timestamp > ?
                GROUP BY model, provider_id
                ORDER BY SUM(total_tokens) DESC
                """,
                (cutoff,),
            ).fetchall()

        return [
            ModelUsage(
                model_id=row[0],
                provider_id=row[1] or "unknown",
                requests=row[2] or 0,
                input_tokens=row[3] or 0,
                output_tokens=row[4] or 0,
                total_tokens=row[5] or 0,
                cost_usd=row[6] or 0.0,
                avg_latency_ms=row[7] or 0.0,
            )
            for row in rows
        ]

    def get_total_cost(self, days: int = 30) -> float:
        """Return total estimated cost over *days* days."""
        cutoff = (_utcnow_naive() - timedelta(days=days)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT SUM(estimated_cost) FROM usage_records WHERE timestamp > ?",
                (cutoff,),
            ).fetchone()
        return row[0] or 0.0

    def get_cache_stats(self, days: int = 30) -> dict:
        """Return aggregated cache token statistics over *days* days."""
        cutoff = (_utcnow_naive() - timedelta(days=days)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT
                    SUM(cache_creation_tokens) AS creation_tokens,
                    SUM(cache_read_tokens)     AS read_tokens,
                    COUNT(CASE WHEN cache_creation_tokens > 0 THEN 1 END) AS cache_writes,
                    COUNT(CASE WHEN cache_read_tokens > 0 THEN 1 END)     AS cache_hits
                FROM usage_records
                WHERE timestamp > ?
                """,
                (cutoff,),
            ).fetchone()
        return {
            "days": days,
            "cache_creation_tokens": row[0] or 0,
            "cache_read_tokens": row[1] or 0,
            "cache_writes": row[2] or 0,
            "cache_hits": row[3] or 0,
        }

    # ------------------------------------------------------------------
    # Historical queries (kept from v1)
    # ------------------------------------------------------------------

    def get_recent_sessions(self, days: int = 7) -> list[UsageSummary]:
        cutoff = (_utcnow_naive() - timedelta(days=days)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            session_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT session_id FROM usage_records WHERE timestamp > ? ORDER BY timestamp DESC",
                    (cutoff,),
                ).fetchall()
            ]
        return [s for s_id in session_ids if (s := self.get_session_summary(s_id))]

    def get_stats_last_n_days(self, days: int = 7) -> dict:
        cutoff = (_utcnow_naive() - timedelta(days=days)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT session_id), SUM(total_tokens),
                       SUM(estimated_cost), COUNT(*)
                FROM usage_records WHERE timestamp > ?
                """,
                (cutoff,),
            ).fetchone()
            most_used = conn.execute(
                """
                SELECT model, SUM(total_tokens)
                FROM usage_records WHERE timestamp > ?
                GROUP BY model ORDER BY SUM(total_tokens) DESC LIMIT 1
                """,
                (cutoff,),
            ).fetchone()

        return {
            "days": days,
            "session_count": row[0] if row else 0,
            "total_tokens": row[1] if row and row[1] else 0,
            "total_cost": row[2] if row and row[2] else 0,
            "completion_count": row[3] if row else 0,
            "most_used_model": most_used[0] if most_used else None,
            "most_used_model_tokens": most_used[1] if most_used else 0,
        }

    def cleanup_old_records(self, days: int = 90) -> int:
        cutoff = (_utcnow_naive() - timedelta(days=days)).isoformat()
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("DELETE FROM usage_records WHERE timestamp < ?", (cutoff,))
                count = cursor.rowcount
                conn.commit()
        if count > 0:
            logger.info("Cleaned up old usage records", count=count, days_old=days)
        return count


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_tracker: SessionUsageTracker | None = None


def get_tracker() -> SessionUsageTracker:
    global _tracker
    if _tracker is None:
        _tracker = SessionUsageTracker()
    return _tracker


def record_usage(
    session_id: str,
    provider_id: str,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: float = 0.0,
    success: bool = True,
    error_code: str | None = None,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> None:
    """Convenience one-liner for the runtime to record a completion."""
    get_tracker().record_completion(
        session_id=session_id,
        model=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        provider_id=provider_id,
        latency_ms=latency_ms,
        success=success,
        error_code=error_code,
        cache_creation_tokens=cache_creation_tokens,
        cache_read_tokens=cache_read_tokens,
    )
