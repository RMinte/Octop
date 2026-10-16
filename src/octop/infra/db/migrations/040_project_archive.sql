-- Project archive state is independent of each task's archive state.
ALTER TABLE projects ADD COLUMN archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1));
ALTER TABLE project_task_turns ADD COLUMN content_blocks_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE project_task_turns ADD COLUMN content_revision BIGINT NOT NULL DEFAULT 0;
UPDATE _schema_version SET version = 40;
