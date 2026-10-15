-- Archive tasks without deleting conversation or execution history.
ALTER TABLE project_tasks ADD COLUMN archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1));
CREATE INDEX IF NOT EXISTS idx_project_tasks_archive_page
  ON project_tasks(owner_id, project_id, archived, updated_at DESC, task_id DESC);
UPDATE _schema_version SET version = CASE WHEN version < 34 THEN 34 ELSE version END;
