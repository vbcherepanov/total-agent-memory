ALTER TABLE knowledge ADD COLUMN state_changed_at TEXT;

CREATE TABLE knowledge_state_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    knowledge_id INTEGER NOT NULL,
    state TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE INDEX idx_knowledge_state_time ON knowledge_state_events(knowledge_id, occurred_at, id);

CREATE TABLE snapshot_embedding_cache (
    knowledge_id INTEGER PRIMARY KEY,
    content_hash TEXT NOT NULL,
    embed_model TEXT NOT NULL,
    embedding_provider TEXT NOT NULL,
    embed_identity_key TEXT NOT NULL,
    embedding_space TEXT NOT NULL,
    float32_vector BLOB NOT NULL
);

INSERT INTO knowledge_state_events(knowledge_id, state, occurred_at)
SELECT k.id,
       CASE WHEN successor.id IS NOT NULL OR k.status='active' THEN 'active' ELSE 'unknown' END,
       COALESCE(k.created_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
FROM knowledge k LEFT JOIN knowledge successor ON successor.id=k.superseded_by;
INSERT INTO knowledge_state_events(knowledge_id, state, occurred_at)
SELECT k.id, COALESCE(k.status, 'unknown'),
       COALESCE(successor.created_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
FROM knowledge k JOIN knowledge successor ON successor.id=k.superseded_by;

CREATE TRIGGER knowledge_state_insert AFTER INSERT ON knowledge
BEGIN
    INSERT INTO knowledge_state_events(knowledge_id, state, occurred_at)
    VALUES (NEW.id, COALESCE(NEW.status, 'unknown'),
            COALESCE(NEW.created_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')));
END;
CREATE TRIGGER knowledge_state_update AFTER UPDATE OF status, superseded_by ON knowledge
WHEN OLD.status IS NOT NEW.status OR OLD.superseded_by IS NOT NEW.superseded_by
BEGIN
    INSERT INTO knowledge_state_events(knowledge_id, state, occurred_at)
    VALUES (NEW.id, COALESCE(NEW.status, 'unknown'), COALESCE(
        CASE WHEN NEW.state_changed_at IS NOT OLD.state_changed_at
             THEN NEW.state_changed_at END,
        CASE WHEN NEW.superseded_by IS NOT OLD.superseded_by
             THEN (SELECT created_at FROM knowledge WHERE id=NEW.superseded_by) END,
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')));
END;
CREATE TRIGGER knowledge_state_delete AFTER DELETE ON knowledge
BEGIN
    DELETE FROM knowledge_state_events WHERE knowledge_id=OLD.id;
    DELETE FROM snapshot_embedding_cache WHERE knowledge_id=OLD.id;
END;
