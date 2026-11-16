CREATE TABLE resource_watches (
    root_id TEXT PRIMARY KEY NOT NULL,
    include_json TEXT NOT NULL,
    exclude_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    signature TEXT NOT NULL DEFAULT '',
    checked_at REAL NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
