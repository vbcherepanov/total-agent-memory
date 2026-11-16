CREATE INDEX IF NOT EXISTS idx_k_similarity_long_prefix
    ON knowledge(project, substr(lower(trim(content)), 1, 24), id);
CREATE INDEX IF NOT EXISTS idx_k_similarity_long_suffix
    ON knowledge(project, CASE WHEN length(trim(content)) >= 24 THEN substr(lower(trim(content)), length(trim(content)) - 23, 24) END, id);
