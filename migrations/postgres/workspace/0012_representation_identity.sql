ALTER TABLE knowledge_representations ADD COLUMN embed_identity_key TEXT;
CREATE INDEX IF NOT EXISTS idx_krepr_identity ON knowledge_representations(embed_identity_key NULLS FIRST, representation);
INSERT INTO migrations (version, description, applied_at) VALUES ('048', 'representation identity', tam_utc_now());
