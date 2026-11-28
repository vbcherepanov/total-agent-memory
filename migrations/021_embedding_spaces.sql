-- v11.0 Phase 1b — Multi-embedding-space metadata.
--
-- Every vector row, both in this SQLite `embeddings` table and in the
-- ChromaDB `knowledge` collection, must record what KIND of content it
-- represents and which embedding model produced it. Without these fields
-- the architecture cannot support a future per-space model swap (e.g.
-- text → BGE-small, code → jina-embeddings-v2-base-code) without a full
-- re-encode of the whole DB.
--
-- The design is documented in docs/archive/v11/audit.md §J.

-- ── New columns ──────────────────────────────────────────────────────
-- ALTER TABLE in SQLite is idempotent only if we guard with the column
-- check ourselves. The Python migration runner already wraps each file in
-- a try/except, so duplicate-column errors on re-run are swallowed there.

ALTER TABLE embeddings ADD COLUMN embedding_provider TEXT;
ALTER TABLE embeddings ADD COLUMN embedding_space    TEXT;
ALTER TABLE embeddings ADD COLUMN content_type       TEXT;
ALTER TABLE embeddings ADD COLUMN language           TEXT;

-- ── Backfill pre-v11 rows ────────────────────────────────────────────
-- Every vector created before v11 came from the text/markdown classifier
-- path (the old code only had one embedder). 'text' is the safe default.
-- 'fastembed' is also safe because v10.x already required FastEmbed for
-- the binary search store (Ollama vectors went into ChromaDB only).

UPDATE embeddings
   SET embedding_space    = COALESCE(embedding_space,    'text'),
       content_type       = COALESCE(content_type,       'text'),
       embedding_provider = COALESCE(embedding_provider, 'fastembed');

-- ── Indexes for filter pushdown ──────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_embeddings_space    ON embeddings(embedding_space);
CREATE INDEX IF NOT EXISTS idx_embeddings_provider ON embeddings(embedding_provider);
