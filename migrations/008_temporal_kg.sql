-- ══════════════════════════════════════════════════════════
-- v7.0 Phase A — Temporal Knowledge Graph
-- Additive log of fact assertions with validity intervals.
-- graph_edges remains "current state", fact_assertions stores history.
-- ══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS fact_assertions (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,              -- graph_nodes.id OR free-form subject string
    predicate TEXT NOT NULL,            -- relation_type
    object TEXT NOT NULL,               -- graph_nodes.id OR free-form object string
    subject_name TEXT,                  -- cached display name
    object_name TEXT,                   -- cached display name
    confidence REAL NOT NULL DEFAULT 1.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    context TEXT,
    source TEXT DEFAULT 'auto',         -- auto|user|llm|migration
    project TEXT DEFAULT 'general',
    valid_from TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    valid_to TEXT,                      -- NULL = currently valid
    superseded_by TEXT REFERENCES fact_assertions(id) ON DELETE SET NULL,
    invalidation_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_fa_spo ON fact_assertions(subject, predicate, object);
CREATE INDEX IF NOT EXISTS idx_fa_subject ON fact_assertions(subject);
CREATE INDEX IF NOT EXISTS idx_fa_object ON fact_assertions(object);
CREATE INDEX IF NOT EXISTS idx_fa_predicate ON fact_assertions(predicate);
CREATE INDEX IF NOT EXISTS idx_fa_valid_from ON fact_assertions(valid_from);
CREATE INDEX IF NOT EXISTS idx_fa_valid_to ON fact_assertions(valid_to);
CREATE INDEX IF NOT EXISTS idx_fa_project ON fact_assertions(project);

-- Partial index for "currently valid" hot queries
CREATE INDEX IF NOT EXISTS idx_fa_current
    ON fact_assertions(subject, predicate, object)
    WHERE valid_to IS NULL;
