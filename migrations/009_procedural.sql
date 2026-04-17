-- ══════════════════════════════════════════════════════════
-- v7.0 Phase B — Procedural memory + outcome tracking
-- Learned workflows with execution history so predictions can improve.
-- ══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    trigger_pattern TEXT,               -- glob/regex or free-form trigger hint
    steps JSON NOT NULL,                -- ["step 1", "step 2", ...]
    context JSON,                       -- tags / prerequisites / stack
    project TEXT DEFAULT 'general',
    source TEXT DEFAULT 'learned',      -- learned|user|imported
    times_run INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    success_rate REAL DEFAULT 0.0,
    avg_duration_ms INTEGER,
    last_run_at TEXT,
    status TEXT DEFAULT 'active',       -- active|deprecated|experimental
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_wf_name ON workflows(name);
CREATE INDEX IF NOT EXISTS idx_wf_project ON workflows(project);
CREATE INDEX IF NOT EXISTS idx_wf_status ON workflows(status);
CREATE INDEX IF NOT EXISTS idx_wf_success_rate ON workflows(success_rate);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    session_id TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('success', 'failure', 'partial', 'aborted')),
    duration_ms INTEGER,
    context JSON,                       -- inputs / environment
    error_details TEXT,                 -- captured error if outcome != success
    notes TEXT,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    ended_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_wfr_workflow ON workflow_runs(workflow_id);
CREATE INDEX IF NOT EXISTS idx_wfr_outcome ON workflow_runs(outcome);
CREATE INDEX IF NOT EXISTS idx_wfr_started ON workflow_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_wfr_session ON workflow_runs(session_id);
