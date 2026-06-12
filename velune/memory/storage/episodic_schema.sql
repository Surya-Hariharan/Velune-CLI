-- Episodic Memory Schema — Phase 2a (storage layer)
-- Applied incrementally by EpisodicMemory._apply_migrations().
-- The Python class owns migration execution; this file is the canonical reference.

-- ── Schema versioning ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS episodic_schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at REAL    NOT NULL
);

-- ── Version 1 ────────────────────────────────────────────────────────────────

-- One row per REPL session (from start_session() to end_session()).
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT    PRIMARY KEY,
    workspace_root TEXT   NOT NULL,
    started_at    REAL    NOT NULL,
    ended_at      REAL,                         -- NULL while session is active
    model_used    TEXT,
    mode          TEXT,
    total_tokens  INTEGER DEFAULT 0,
    summary       TEXT                          -- LLM-generated; added at session end
);

-- One row per conversation turn (user or assistant message).
CREATE TABLE IF NOT EXISTS turns (
    id           TEXT    PRIMARY KEY,
    session_id   TEXT    NOT NULL REFERENCES sessions(id),
    turn_index   INTEGER NOT NULL,              -- 0-based position within session
    role         TEXT    NOT NULL,              -- 'user' or 'assistant'
    content      TEXT    NOT NULL,
    model_used   TEXT,
    tokens_used  INTEGER,
    created_at   REAL    NOT NULL,
    embedding_id TEXT                           -- set after embedding in Phase 2a-2
);

-- Structured annotations on turns (e.g. 'architectural_decision', 'bug_fix').
CREATE TABLE IF NOT EXISTS memory_tags (
    turn_id TEXT NOT NULL REFERENCES turns(id),
    tag     TEXT NOT NULL,
    value   TEXT,                               -- optional structured payload
    PRIMARY KEY (turn_id, tag)
);

-- ── Indexes ───────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_sessions_workspace ON sessions(workspace_root);
CREATE INDEX IF NOT EXISTS idx_sessions_started   ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_turns_session      ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_turns_created      ON turns(created_at);
CREATE INDEX IF NOT EXISTS idx_turns_role         ON turns(role);
CREATE INDEX IF NOT EXISTS idx_tags_turn          ON memory_tags(turn_id);
CREATE INDEX IF NOT EXISTS idx_tags_tag           ON memory_tags(tag);
