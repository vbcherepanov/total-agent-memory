UPDATE rules
   SET project = COALESCE(NULLIF(btrim(project, E' \t\r\n'), ''), 'general'),
       scope = CASE
           WHEN COALESCE(NULLIF(btrim(project, E' \t\r\n'), ''), 'general') = 'general' THEN 'global'
           ELSE 'project:' || btrim(project, E' \t\r\n')
       END
 WHERE session_id = 'auto_consolidate'
   AND project IS NOT NULL
   AND project <> btrim(project, E' \t\r\n');

INSERT INTO migrations (version, description, applied_at)
VALUES ('057', 'auto rule scope whitespace', tam_utc_now())
ON CONFLICT (version) DO NOTHING;
