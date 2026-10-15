-- Existing titles stay untouched; only newly admitted tasks enqueue naming work.
ALTER TABLE project_tasks ADD COLUMN title_source TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE project_tasks ADD COLUMN title_revision BIGINT NOT NULL DEFAULT 1;
CREATE TABLE IF NOT EXISTS project_task_titles (
  task_id TEXT PRIMARY KEY REFERENCES project_tasks(task_id),
  title_revision BIGINT NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'running', 'completed', 'failed', 'skipped')),
  worker_id TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_until BIGINT NOT NULL DEFAULT 0,
  input_tokens BIGINT NOT NULL DEFAULT 0,
  output_tokens BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_task_titles_queue ON project_task_titles(status, lease_until);
UPDATE _schema_version SET version = 36;
