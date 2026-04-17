-- ══════════════════════════════════════════════════════════
-- v7.0 Phase G — Session continuity
-- Stores end-of-session summaries and next-step items so a new session
-- can pick up exactly where the previous one left off.
-- ══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS session_summaries (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    project TEXT DEFAULT 'general',
    branch TEXT,
    summary TEXT NOT NULL,
    highlights JSON,                    -- ["accomplishment 1", ...]
    pitfalls JSON,                      -- warnings to carry forward
    next_steps JSON,                    -- ["todo 1", "todo 2", ...]
    open_questions JSON,
    context_blob TEXT,                  -- free-form context carry-over
    started_at TEXT,
    ended_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    consumed INTEGER DEFAULT 0,         -- has next session picked this up?
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_ss_project ON session_summaries(project);
CREATE INDEX IF NOT EXISTS idx_ss_ended_at ON session_summaries(ended_at DESC);
CREATE INDEX IF NOT EXISTS idx_ss_consumed ON session_summaries(consumed);
CREATE INDEX IF NOT EXISTS idx_ss_session ON session_summaries(session_id);
