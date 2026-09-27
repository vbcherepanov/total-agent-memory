-- 038_resolve_learned_errors.sql
-- Closes `errors` rows that `ErrorCapture.learn_error` wrote as 'open'
-- although it requires a fix. Only its rows are touched: their context
-- starts with the exact, case-sensitive 'root_cause:' of its
-- 'root_cause: ... | pattern: ...' signature. resolved_at takes the row's
-- created_at, when the fix was already known. Rows without a fix stay open.

UPDATE errors
   SET status = 'resolved',
       resolved_at = created_at
 WHERE status = 'open'
   AND COALESCE(fix, '') <> ''
   AND substr(context, 1, 11) = 'root_cause:';
