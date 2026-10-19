-- Record observed execution boundaries only; historical timing stays unknown.
ALTER TABLE project_task_turns ADD COLUMN started_at BIGINT;
ALTER TABLE project_task_turns ADD COLUMN finished_at BIGINT;
UPDATE _schema_version SET version = 44;
