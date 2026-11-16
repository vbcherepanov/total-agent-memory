-- Repair of the 0018 backfill; same semantics as SQLite migration 058.
DROP TABLE IF EXISTS pg_temp.knowledge_state_repair;
CREATE TEMP TABLE knowledge_state_repair AS
SELECT k.id AS knowledge_id, k.status AS state,
       CASE
           WHEN s.created_at IS NOT NULL AND tam_compat.julianday(s.created_at) > tam_compat.julianday(k.created_at)
               THEN s.created_at
           WHEN k.last_confirmed IS NOT NULL AND tam_compat.julianday(k.last_confirmed) > tam_compat.julianday(k.created_at)
               THEN k.last_confirmed
       END AS retired_at
FROM knowledge k LEFT JOIN knowledge s ON s.id = k.superseded_by
WHERE k.status IS NOT NULL AND k.status NOT IN ('active', 'unknown')
  AND (SELECT count(*) FROM knowledge_state_events e WHERE e.knowledge_id = k.id) = 1
  AND EXISTS (SELECT 1 FROM knowledge_state_events e WHERE e.knowledge_id = k.id AND e.state = 'unknown');
DELETE FROM knowledge_state_repair WHERE retired_at IS NULL;
UPDATE knowledge_state_events SET state = 'active'
WHERE state = 'unknown' AND knowledge_id IN (SELECT knowledge_id FROM knowledge_state_repair);
INSERT INTO knowledge_state_events(knowledge_id, state, occurred_at)
SELECT knowledge_id, state, retired_at FROM knowledge_state_repair ORDER BY knowledge_id;
DROP TABLE knowledge_state_repair;

INSERT INTO migrations(version, description, applied_at)
VALUES ('058', 'knowledge state retirement backfill', tam_utc_now())
ON CONFLICT(version) DO NOTHING;
