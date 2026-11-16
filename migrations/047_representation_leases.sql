ALTER TABLE representations_queue ADD COLUMN next_attempt_at TEXT;
ALTER TABLE representations_queue ADD COLUMN claim_token TEXT;
CREATE INDEX IF NOT EXISTS idx_repq_due ON representations_queue(status, next_attempt_at);
