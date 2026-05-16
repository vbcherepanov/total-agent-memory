-- Migration 027: Per-representation freshness tracking (Wave B — B1).
--
-- Adds two columns to knowledge_representations:
--   parent_content_hash  — sha256 of the parent knowledge.content at view-gen
--                          time. Used by recall to detect drift: if the parent
--                          content has changed since this view was generated,
--                          score for hits via this representation is dampened
--                          and the view is re-enqueued for regeneration.
--   last_confirmed       — independent timestamp per view (separate from
--                          knowledge.last_confirmed). Reserved for future
--                          per-view recency boosts; populated on upsert.
--
-- Backward compatible: NULL means "legacy view, no drift info known".

ALTER TABLE knowledge_representations
    ADD COLUMN parent_content_hash TEXT;

ALTER TABLE knowledge_representations
    ADD COLUMN last_confirmed TEXT;

CREATE INDEX IF NOT EXISTS idx_krepr_hash
    ON knowledge_representations(parent_content_hash);
