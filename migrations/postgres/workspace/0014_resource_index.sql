CREATE TABLE resource_roots (
    id TEXT PRIMARY KEY NOT NULL,
    project TEXT NOT NULL,
    path TEXT NOT NULL,
    revision BIGINT NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE(project, path)
);
CREATE TABLE resource_files (
    id TEXT PRIMARY KEY NOT NULL,
    root_id TEXT NOT NULL,
    path TEXT NOT NULL,
    digest TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    language TEXT NOT NULL,
    media_type TEXT NOT NULL,
    encoding TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    UNIQUE(root_id, path)
);
CREATE INDEX idx_resource_files_root ON resource_files(root_id);
CREATE TABLE resource_chunks (
    id TEXT PRIMARY KEY NOT NULL,
    file_id TEXT NOT NULL,
    ordinal BIGINT NOT NULL,
    kind TEXT NOT NULL,
    language TEXT NOT NULL,
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    start_line BIGINT NOT NULL,
    end_line BIGINT NOT NULL,
    UNIQUE(file_id, ordinal)
);
CREATE INDEX idx_resource_chunks_file ON resource_chunks(file_id);
CREATE TABLE resource_terms (
    term TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    PRIMARY KEY(term, chunk_id)
);
CREATE INDEX idx_resource_terms_chunk ON resource_terms(chunk_id);

INSERT INTO migrations(version,description,applied_at) VALUES ('050','resource index',tam_utc_now());
