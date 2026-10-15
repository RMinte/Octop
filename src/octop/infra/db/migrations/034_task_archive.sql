-- Archive tasks without deleting conversation or execution history.
ALTER TABLE project_tasks ADD COLUMN archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1));
CREATE INDEX IF NOT EXISTS idx_project_tasks_archive_page
  ON project_tasks(owner_id, project_id, archived, updated_at DESC, task_id DESC);
ALTER TABLE project_tasks ADD COLUMN status TEXT NOT NULL DEFAULT 'queued'
  CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled'));
UPDATE project_tasks SET status=COALESCE(
  (SELECT status FROM project_task_turns
   WHERE task_id=project_tasks.task_id
     AND (project_tasks.active_turn_id IS NULL OR turn_id=project_tasks.active_turn_id)
   ORDER BY id DESC LIMIT 1), 'queued');
UPDATE _schema_version SET version = CASE WHEN version < 34 THEN 34 ELSE version END;
