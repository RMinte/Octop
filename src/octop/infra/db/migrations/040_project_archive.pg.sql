-- Project archive state is independent of each task's archive state.
ALTER TABLE projects ADD COLUMN archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1));
UPDATE _schema_version SET version = 40;
