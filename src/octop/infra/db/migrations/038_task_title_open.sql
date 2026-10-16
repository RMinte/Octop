-- Keep attempt numbers monotonic when task.open grants a new naming attempt.
ALTER TABLE project_task_titles ADD COLUMN attempt_limit INTEGER NOT NULL DEFAULT 2;
UPDATE _schema_version SET version = 38;
