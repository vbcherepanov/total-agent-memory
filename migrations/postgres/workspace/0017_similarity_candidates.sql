CREATE INDEX IF NOT EXISTS idx_k_similarity_prefix
    ON knowledge(project NULLS FIRST, substr(lower(trim(content)), 1, 3) NULLS FIRST, id);
CREATE INDEX IF NOT EXISTS idx_k_similarity_suffix
    ON knowledge(project NULLS FIRST, substr(lower(trim(content)), length(trim(content)) - 2, 3) NULLS FIRST, id);
INSERT INTO migrations(version, description, applied_at)
VALUES ('054', 'similarity candidates', tam_utc_now())
ON CONFLICT(version) DO NOTHING;
