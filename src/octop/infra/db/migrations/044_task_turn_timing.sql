-- Record observed execution boundaries only; historical timing stays unknown.
ALTER TABLE project_task_turns ADD COLUMN started_at INTEGER;
ALTER TABLE project_task_turns ADD COLUMN finished_at INTEGER;
UPDATE _schema_version SET version = 44;
