-- Repair of the 055 backfill. A record that was already retired (deleted,
-- archived, purged, consolidated, superseded without a surviving successor...)
-- before the upgrade got one 'unknown' event and was invisible at every as_of.
-- Rewrite only histories that are exactly that single backfill event: the
-- record becomes 'active' from created_at and takes its current status at the
-- best retirement time the schema holds: the successor's created_at, else
-- last_confirmed, each only when later than created_at. With neither, the
-- single 'unknown' event is left as is. Histories written by triggers have
-- more than one event or a non-'unknown' state and are never touched.
DROP TABLE IF EXISTS temp.knowledge_state_repair;
CREATE TEMP TABLE knowledge_state_repair AS
SELECT k.id AS knowledge_id, k.status AS state,
       CASE
           WHEN s.created_at IS NOT NULL AND julianday(s.created_at) > julianday(k.created_at)
               THEN s.created_at
           WHEN k.last_confirmed IS NOT NULL AND julianday(k.last_confirmed) > julianday(k.created_at)
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
