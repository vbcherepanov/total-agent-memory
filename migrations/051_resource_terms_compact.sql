CREATE TABLE resource_terms_compact (
    term TEXT NOT NULL,
    chunk_id TEXT NOT NULL REFERENCES resource_chunks(id) ON DELETE CASCADE,
    PRIMARY KEY(term, chunk_id)
) WITHOUT ROWID;
INSERT INTO resource_terms_compact(term, chunk_id)
SELECT term, chunk_id FROM resource_terms;
DROP TABLE resource_terms;
ALTER TABLE resource_terms_compact RENAME TO resource_terms;
CREATE INDEX idx_resource_terms_chunk ON resource_terms(chunk_id);
