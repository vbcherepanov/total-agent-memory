CREATE TABLE resource_watches (
    root_id TEXT PRIMARY KEY NOT NULL,
    include_json TEXT NOT NULL,
    exclude_json TEXT NOT NULL,
    enabled BIGINT NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    signature TEXT NOT NULL DEFAULT '',
    checked_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
INSERT INTO migrations(version,description,applied_at) VALUES ('051','resource terms compact',tam_utc_now());
INSERT INTO migrations(version,description,applied_at) VALUES ('052','resource watches',tam_utc_now());
