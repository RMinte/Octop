-- Independent tasks account for tokens without creating an agent/expert row.
ALTER TABLE project_task_turns ADD COLUMN input_tokens BIGINT NOT NULL DEFAULT 0;
ALTER TABLE project_task_turns ADD COLUMN output_tokens BIGINT NOT NULL DEFAULT 0;
UPDATE _schema_version SET version = 31;
