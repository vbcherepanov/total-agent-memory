UPDATE rules
   SET project = COALESCE(NULLIF(TRIM(project, ' ' || char(9) || char(10) || char(13)), ''), 'general'),
       scope = CASE
           WHEN COALESCE(NULLIF(TRIM(project, ' ' || char(9) || char(10) || char(13)), ''), 'general') = 'general' THEN 'global'
           ELSE 'project:' || TRIM(project, ' ' || char(9) || char(10) || char(13))
       END
 WHERE session_id = 'auto_consolidate'
   AND project IS NOT NULL
   AND project <> TRIM(project, ' ' || char(9) || char(10) || char(13));
