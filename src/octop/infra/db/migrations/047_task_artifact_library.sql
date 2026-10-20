-- Published-file browsing is ordered by publication time, including task filters.
CREATE INDEX IF NOT EXISTS idx_task_artifacts_created
    ON project_task_artifacts(created_at DESC, artifact_id DESC);
CREATE INDEX IF NOT EXISTS idx_task_artifacts_task_created
    ON project_task_artifacts(task_id, created_at DESC, artifact_id DESC);
UPDATE _schema_version SET version = 47;
