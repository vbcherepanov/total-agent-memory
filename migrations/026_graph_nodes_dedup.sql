-- Migration 026 — graph_nodes name normalization for dedup.
--
-- Background: graph_nodes had no UNIQUE on (name, type); name lookups in
-- add_node were case-sensitive. Result on production DBs: same entity
-- ("vue" / "Vue" / "Vue.js") stored as multiple rows; concurrent workers
-- racing on extract produced orphan duplicates because get-then-insert is
-- not atomic.
--
-- This migration is non-destructive:
--   * adds `name_norm` (= lower(trim(name)))
--   * backfills existing rows
--   * keeps triggers in sync on INSERT/UPDATE so application code can stay
--     simple
--   * adds a non-unique index for case-insensitive lookup
--
-- The actual UNIQUE constraint is added by tools/merge_duplicate_nodes.py
-- AFTER duplicates have been merged. This split keeps the migration safe
-- to apply on any existing database.

-- 1. Column. SQLite is permissive about re-adding columns guarded by
--    EXCEPT, but it errors on the second apply without protection. The
--    migration runner (server._apply_sql_migrations) records applied
--    versions, so we rely on that and use a plain ALTER TABLE.
ALTER TABLE graph_nodes ADD COLUMN name_norm TEXT;

-- 2. Backfill. trim+lower mirrors the canonicalization in add_node().
UPDATE graph_nodes
SET name_norm = lower(trim(name))
WHERE name_norm IS NULL;

-- 3. Index — case-insensitive lookup hot path.
CREATE INDEX IF NOT EXISTS idx_graph_nodes_name_norm
    ON graph_nodes(name_norm);

-- 4. Composite index for the most common pattern: lookup by
--    (name_norm, type) without scanning all rows that share a name.
CREATE INDEX IF NOT EXISTS idx_graph_nodes_name_norm_type
    ON graph_nodes(name_norm, type);

-- 5. Triggers to keep name_norm in sync. Without these, future inserts
--    via raw SQL (tests, migrations, ad-hoc scripts) would leave NULLs.
CREATE TRIGGER IF NOT EXISTS graph_nodes_name_norm_ins
AFTER INSERT ON graph_nodes
WHEN NEW.name_norm IS NULL OR NEW.name_norm != lower(trim(NEW.name))
BEGIN
    UPDATE graph_nodes
    SET name_norm = lower(trim(NEW.name))
    WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS graph_nodes_name_norm_upd
AFTER UPDATE OF name ON graph_nodes
WHEN NEW.name_norm IS NULL OR NEW.name_norm != lower(trim(NEW.name))
BEGIN
    UPDATE graph_nodes
    SET name_norm = lower(trim(NEW.name))
    WHERE id = NEW.id;
END;
