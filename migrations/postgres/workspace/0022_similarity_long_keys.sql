CREATE INDEX IF NOT EXISTS idx_k_similarity_long_prefix
    ON knowledge(project NULLS FIRST, substr(lower(trim(content)), 1, 24) NULLS FIRST, id);
CREATE INDEX IF NOT EXISTS idx_k_similarity_long_suffix
    ON knowledge(project NULLS FIRST, (CASE WHEN length(trim(content)) >= 24 THEN substr(lower(trim(content)), length(trim(content)) - 23, 24) END) NULLS FIRST, id);
INSERT INTO migrations(version, description, applied_at)
VALUES ('059', 'similarity long keys', tam_utc_now())
ON CONFLICT(version) DO NOTHING;
