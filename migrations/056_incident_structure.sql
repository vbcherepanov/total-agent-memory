CREATE TABLE knowledge_incidents (
    knowledge_id INTEGER PRIMARY KEY,
    detection_check TEXT,
    healthy_result TEXT,
    resolution TEXT,
    incident_status TEXT NOT NULL DEFAULT 'open' CHECK (incident_status IN ('open', 'closed')),
    open_items INTEGER NOT NULL DEFAULT 0 CHECK (open_items >= 0),
    CHECK (incident_status != 'closed' OR (resolution IS NOT NULL AND length(trim(resolution)) > 0))
);
CREATE INDEX idx_incidents_status ON knowledge_incidents(incident_status, knowledge_id);
CREATE TABLE knowledge_incident_windows (
    knowledge_id INTEGER NOT NULL,
    scope TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    PRIMARY KEY(knowledge_id, scope, starts_at),
    CHECK (ends_at IS NULL OR ends_at > starts_at)
);
CREATE INDEX idx_incident_windows_scope ON knowledge_incident_windows(scope, starts_at, ends_at, knowledge_id);
INSERT INTO knowledge_incidents(knowledge_id) SELECT id FROM knowledge WHERE type='incident';

CREATE TRIGGER knowledge_incident_delete AFTER DELETE ON knowledge
BEGIN
    DELETE FROM knowledge_incident_windows WHERE knowledge_id=OLD.id;
    DELETE FROM knowledge_incidents WHERE knowledge_id=OLD.id;
END;
