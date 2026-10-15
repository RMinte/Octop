-- Separate project and per-project task synchronization streams.
ALTER TABLE project_tasks ADD COLUMN list_revision BIGINT NOT NULL DEFAULT 1;
CREATE INDEX IF NOT EXISTS idx_project_tasks_scope_page
  ON project_tasks(owner_id, project_id, updated_at DESC, task_id DESC);
CREATE TABLE IF NOT EXISTS project_sync_scopes (
  owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  scope TEXT NOT NULL,
  sequence BIGINT NOT NULL DEFAULT 0,
  PRIMARY KEY (owner_id, scope)
);
CREATE TABLE IF NOT EXISTS project_sync_changes (
  owner_id INTEGER NOT NULL,
  scope TEXT NOT NULL,
  sequence BIGINT NOT NULL,
  entity_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (owner_id, scope, sequence),
  FOREIGN KEY (owner_id, scope) REFERENCES project_sync_scopes(owner_id, scope) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_project_sync_changes_entity
  ON project_sync_changes(owner_id, scope, entity_id, sequence DESC);
UPDATE _schema_version SET version = CASE WHEN version < 33 THEN 33 ELSE version END;
