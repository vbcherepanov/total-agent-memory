CREATE INDEX IF NOT EXISTS idx_k_similarity_prefix
    ON knowledge(project, substr(lower(trim(content)), 1, 3), id);
CREATE INDEX IF NOT EXISTS idx_k_similarity_suffix
    ON knowledge(project, substr(lower(trim(content)), length(trim(content)) - 2, 3), id);
