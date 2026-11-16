UPDATE rules
   SET project = COALESCE(NULLIF(TRIM(project), ''), 'general'),
       scope = CASE
           WHEN COALESCE(NULLIF(TRIM(project), ''), 'general') = 'general' THEN 'global'
           ELSE 'project:' || TRIM(project)
       END
 WHERE scope = 'project' AND session_id = 'auto_consolidate';
