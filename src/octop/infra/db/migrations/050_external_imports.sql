-- External data is copied into owner-scoped projects and tasks; configuration stays inert.
ALTER TABLE project_tasks ADD COLUMN import_source TEXT NOT NULL DEFAULT '';
ALTER TABLE project_tasks ADD COLUMN external_session_id TEXT NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS external_import_projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id INTEGER NOT NULL REFERENCES users(id),
  source TEXT NOT NULL CHECK (source IN ('codex', 'claude')),
  external_project_id TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  UNIQUE (owner_id, source, external_project_id)
);
CREATE TABLE IF NOT EXISTS external_import_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id INTEGER NOT NULL REFERENCES users(id),
  source TEXT NOT NULL CHECK (source IN ('codex', 'claude')),
  external_session_id TEXT NOT NULL,
  task_id TEXT NOT NULL UNIQUE REFERENCES project_tasks(task_id) ON DELETE CASCADE,
  fingerprint TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  UNIQUE (owner_id, source, external_session_id)
);
CREATE TABLE IF NOT EXISTS external_import_configurations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  configuration_id TEXT NOT NULL UNIQUE,
  owner_id INTEGER NOT NULL REFERENCES users(id),
  source TEXT NOT NULL CHECK (source IN ('codex', 'claude')),
  category TEXT NOT NULL CHECK (category IN ('settings', 'instructions', 'plugins', 'skills', 'mcpServers')),
  name TEXT NOT NULL,
  source_path TEXT,
  digest TEXT NOT NULL,
  byte_count BIGINT NOT NULL CHECK (byte_count >= 0 AND byte_count <= 20971520),
  file_count INTEGER NOT NULL CHECK (file_count > 0 AND file_count <= 1000),
  files_json TEXT NOT NULL,
  created_at BIGINT NOT NULL,
  UNIQUE (owner_id, source, digest)
);
CREATE INDEX IF NOT EXISTS idx_external_import_configurations_owner
  ON external_import_configurations(owner_id, created_at DESC, configuration_id DESC);
UPDATE _schema_version SET version = CASE WHEN version < 50 THEN 50 ELSE version END;
